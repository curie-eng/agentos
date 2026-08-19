"""A write connector that can do exactly one thing: restart a deployment.

Why this exists rather than `kubernetes-mcp-server` without `--read-only`
------------------------------------------------------------------------
Dropping `--read-only` from the existing image would expose create/update/delete
across every resource kind. The bot would then hold general write, gated only by
what the approval policy remembers to name -- and a gate is armed by exact tool
name, so a tool nobody thought to gate is a tool with nothing in front of it.

This server exposes ONE tool. There is no second thing for a gate to miss.

The constraint that actually matters
------------------------------------
`kubectl rollout restart` is not a distinct Kubernetes permission. It is a PATCH
of the Deployment's pod template, so RBAC `patch` on deployments is the same
grant as `set image`, `set env`, and replacing the container command. There is no
RBAC expression that separates them (see manifests/write-role.yaml).

So the separation has to live here, and it is structural rather than advisory:

* The tool takes `namespace` and `name`. Nothing else. There is no parameter
  through which a caller could reach an image, an env var, a command, or a
  replica count.
* The patch body is a CONSTANT in this file, with only a timestamp filled in. A
  caller-supplied patch is never forwarded -- there is no code path that could.
* An allowlist is checked before the request is built, so even a compromised
  caller reaches only workloads an operator named.

The agent is prompt-injectable. This process is not. That is the same reasoning
that keeps the Grafana token out of the sandbox, applied to a verb instead of a
credential.

Read-back is deliberately NOT here
----------------------------------
Verifying the rollout afterwards is a read, and the read-only Kubernetes
connector already does it better (it can see pods, events, and previous-container
logs). Adding a read tool here would widen this server's surface for no gain and
give the write identity a reason to hold `list`. The bot restarts with this and
verifies with the other one -- which is also what makes the verification
meaningful, since it comes from a different credential.
"""

# NOTE: no `from __future__ import annotations`. FastMCP introspects tool
# signatures with `issubclass(param.annotation, Context)`, and stringized
# annotations make that raise `TypeError: issubclass() arg 1 must be a class` at
# import time -- before the server binds a port. Learned on the tempo connector.

import base64
import json
import logging
import os
import ssl
import sys
import time
from typing import Any

import httpx
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

log = logging.getLogger("k8s-write-mcp")

KUBECONFIG = os.environ.get("KUBECONFIG_PATH", "/secrets/kubeconfig")
TIMEOUT = float(os.environ.get("K8S_TIMEOUT_SECONDS", "30"))

# "namespace/name,namespace/name". Empty means nothing is permitted -- the
# server starts, answers, and refuses every call. That is the correct default
# for a write path: an operator opts workloads in by name, and a missing config
# fails closed rather than granting everything.
ALLOWLIST = frozenset(
    entry.strip()
    for entry in os.environ.get("K8S_WRITE_ALLOWLIST", "").split(",")
    if entry.strip()
)

mcp = FastMCP(
    "k8s-write",
    host=os.environ.get("BIND_ADDRESS", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    # Mount at "/" so both /mcp and /mcp/ are served. FastMCP's default 307s a
    # POST from /mcp to /mcp/, and an MCP client is not obliged to replay a POST
    # body across a redirect -- the symptom is a connector that is up, healthy,
    # answers curl, and registers zero tools.
    streamable_http_path="/",
)

# readOnlyHint=False is the honest value and the point of this server.
# destructiveHint=False: a rolling restart replaces pods under the Deployment's
# own surge/availability rules; it does not delete anything the way `pods delete`
# would. idempotentHint=False is load-bearing -- each call starts a NEW rollout,
# so a client that retries on a timeout would roll twice. Nothing here may be
# auto-retried.
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def _client() -> Any:
    """Build an httpx client from the mounted kubeconfig, or return a string.

    The kubeconfig is a static one built for this identity (the bundle README's
    write-path section has the assembly steps).
    Parsed here rather than pulling in the full Kubernetes client, so the
    failure modes are ones this file can explain.
    """

    try:
        with open(KUBECONFIG, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError:
        return f"no kubeconfig at {KUBECONFIG}: the write credential is not mounted"
    except (OSError, yaml.YAMLError) as exc:
        return f"could not read the kubeconfig at {KUBECONFIG}: {exc}"

    try:
        cluster = cfg["clusters"][0]["cluster"]
        user = cfg["users"][0]["user"]
        server = cluster["server"]
        token = user["token"]
    except (KeyError, IndexError, TypeError):
        return "kubeconfig is missing a cluster server or a user token"

    # Both CA shapes, because a kubeconfig may carry either and getting this
    # wrong fails as CERTIFICATE_VERIFY_FAILED -- which reads like a cluster
    # problem rather than a parsing one. An `aws eks update-kubeconfig` config
    # and every hand-assembled one here use the INLINE `-data` form; only a
    # file-path config uses the other. Handling just the path form (the first
    # version of this file) meant `verify` silently stayed True, httpx checked
    # the EKS certificate against the system trust store, and every write failed
    # after the approval had already been given.
    verify: Any = True
    ca_data = cluster.get("certificate-authority-data")
    ca_path = cluster.get("certificate-authority")
    if ca_data:
        # IN MEMORY, not a temp file. Curie hardens every connector with a
        # read-only root filesystem and mounts no writable scratch here, so
        # `tempfile` raises "no usable temporary directory" -- which surfaced as
        # a tool-side error AFTER a human had approved the write, twice. An
        # SSLContext takes the PEM directly and touches no filesystem.
        try:
            pem = base64.b64decode(ca_data).decode("ascii")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return f"kubeconfig certificate-authority-data is not a valid base64 PEM: {exc}"
        ctx = ssl.create_default_context()
        try:
            ctx.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            return f"kubeconfig certificate-authority-data is not a usable CA certificate: {exc}"
        verify = ctx
    elif ca_path:
        verify = ca_path
    elif cluster.get("insecure-skip-tls-verify"):
        # Never silently. A write path that skips verification is a write path
        # that can be pointed at an impostor API server.
        return (
            "kubeconfig sets insecure-skip-tls-verify; refusing to write over an "
            "unverified connection"
        )

    return httpx.Client(
        base_url=server,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
        timeout=TIMEOUT,
    )


@mcp.tool(annotations=WRITE)
def restart_deployment(namespace: str, name: str) -> str:
    """Roll a Deployment's pods by triggering a rollout restart.

    Equivalent to `kubectl -n <namespace> rollout restart deploy/<name>`: the pod
    template gets a fresh annotation, so the Deployment creates new pods and
    retires the old ones under its own surge and availability settings.

    This is a WRITE and it is gated -- the call pauses the turn for human
    approval before anything happens. Say what you are about to restart and why
    before calling it.

    NOT idempotent. Each call starts a new rollout. If this times out or errors,
    do NOT call it again: check the rollout's state with the read-only Kubernetes
    tools first, because the restart may well have started.

    Verify afterwards with the read-only connector (`pods_list`, `events_list`)
    rather than assuming success from this tool's return value.
    """

    namespace = (namespace or "").strip()
    name = (name or "").strip()
    if not namespace or not name:
        return "both namespace and name are required"

    target = f"{namespace}/{name}"
    if target not in ALLOWLIST:
        permitted = ", ".join(sorted(ALLOWLIST)) or "(none configured)"
        return (
            f"refusing: {target} is not in this connector's allowlist. "
            f"Permitted: {permitted}. This is a deliberate ceiling -- widening it "
            "is an operator change, not something to work around."
        )

    client = _client()
    if isinstance(client, str):
        return client

    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
    try:
        with client:
            existing = client.get(path)
            if existing.status_code == 404:
                return f"no Deployment {target}. Check the name with the read-only tools."
            if existing.status_code in (401, 403):
                return (
                    f"the API server refused the read ({existing.status_code}). The write "
                    "credential lacks access to this Deployment. This will not fix itself on retry."
                )
            if existing.status_code >= 400:
                return f"could not read {target}: {existing.status_code} {existing.text[:200]}"

            # The patch body is constant apart from the timestamp. Nothing a
            # caller supplied reaches it.
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": stamp}
                        }
                    }
                }
            }
            applied = client.patch(
                path,
                content=json.dumps(patch),
                headers={"Content-Type": "application/strategic-merge-patch+json"},
            )
    except httpx.TimeoutException:
        return (
            f"the API server did not respond within {TIMEOUT:g}s. The restart MAY have "
            "started -- check the rollout with the read-only tools before doing anything else. "
            "Do not call this again."
        )
    except httpx.HTTPError as exc:
        return f"could not reach the API server: {exc}"

    if applied.status_code in (401, 403):
        return (
            f"the API server refused the patch ({applied.status_code}). The write credential "
            "can read this Deployment but not patch it. This will not fix itself on retry."
        )
    if applied.status_code >= 400:
        return f"the restart was rejected: {applied.status_code} {applied.text[:200]}"

    return (
        f"restart triggered for {target} at {stamp}. New pods are rolling out under the "
        "Deployment's own surge settings. Confirm with the read-only tools before reporting "
        "it as finished -- this return value means the patch was accepted, not that the "
        "rollout completed."
    )


def main() -> int:
    if not ALLOWLIST:
        # Starting with an empty allowlist would look healthy and refuse every
        # call, which reads as a broken bot rather than a missing config.
        log.error(
            "K8S_WRITE_ALLOWLIST is empty; refusing to start a write connector "
            "that permits nothing"
        )
        return 1
    if not os.path.exists(KUBECONFIG):
        log.error("no kubeconfig at %s; refusing to start without a credential", KUBECONFIG)
        return 1
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
