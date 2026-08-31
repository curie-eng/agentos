"""A write connector that can do exactly one thing: set a Deployment's replicas.

Why a second server rather than a second tool on `k8s-write`
------------------------------------------------------------
`k8s-write`'s own docstring is the reason: "The tool takes `namespace` and
`name`. Nothing else. There is no parameter through which a caller could reach an
image, an env var, a command, or a replica count." Adding a scale tool there
would put caller input into a process whose entire argument is that no caller
input reaches the patch body. The same file also says "This server exposes ONE
tool. There is no second thing for a gate to miss." Both hold here, so scale gets
its own server and keeps the one-tool posture.

The constraint that actually matters, and why this one is NARROWER than restart
------------------------------------------------------------------------------
A rollout restart is a PATCH of the Deployment's pod template, so RBAC `patch` on
`deployments` is the same grant as `set image` and replacing the container
command. There is no RBAC expression that separates them, which is why
`k8s-write` has to enforce the separation in Python.

Scaling does not have that problem. Kubernetes exposes `deployments/scale` as its
own subresource, so an operator can grant `patch` on `deployments/scale` and
nothing else. That grant cannot change an image, a command, or an env var --
Kubernetes itself refuses, not this file. `replicas` is a caller parameter here
because it is the verb's argument rather than a channel into an arbitrary patch,
and the subresource is what makes that safe.

REVERSIBLE, which is the other reason this exists
-------------------------------------------------
Unlike a restart, a scale can be put back: the replica count that was in force
immediately before the patch is enough to restore it. This tool reads that count
on the way past and returns it in a structured reply, so the platform can record
what to restore without asking a model what happened. A restart cannot do this,
and says so.

The reply is JSON rather than prose. A tool that reports state a machine will act
on has to be parseable, and the model reads JSON perfectly well.
"""

# NOTE: no `from __future__ import annotations`. FastMCP introspects tool
# signatures with `issubclass(param.annotation, Context)`, and stringized
# annotations make that raise `TypeError` at import time.

import base64
import json
import logging
import os
import ssl
from typing import Any

import httpx
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

log = logging.getLogger("k8s-scale-mcp")

KUBECONFIG = os.environ.get("KUBECONFIG_PATH", "/secrets/kubeconfig")
TIMEOUT = float(os.environ.get("K8S_TIMEOUT_SECONDS", "30"))

# "namespace/name,namespace/name". Empty means nothing is permitted.
ALLOWLIST = frozenset(
    entry.strip()
    for entry in os.environ.get("K8S_SCALE_ALLOWLIST", "").split(",")
    if entry.strip()
)

# A ceiling an operator sets, because "scale to 10000" is a denial-of-service
# with an approval on it. Refused before a client is built, like the allowlist.
MAX_REPLICAS = int(os.environ.get("K8S_SCALE_MAX_REPLICAS", "50"))

mcp = FastMCP(
    "k8s-scale",
    host=os.environ.get("BIND_ADDRESS", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    # Curie addresses hosted connectors at the exact, redirect-free /mcp path.
    # MCP 1.28 mounts this value literally; "/" would leave /mcp returning 404.
    streamable_http_path="/mcp",
)

# idempotentHint=True is the honest value and the difference from restart:
# setting replicas to 3 twice leaves the same cluster state. It is still a write
# and still gated; it just does not compound the way a rollout does.
SCALE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _reply(ok, summary, prior=None, post=None, target=None):
    """Return the successful reversible-snapshot contract as structured JSON.

    `prior` is what a restore puts back. `post` is what a restore is checked
    AGAINST -- the platform refuses to restore when the live resource no longer
    matches what this call left (ADR-0117 decision 4). They are not
    interchangeable: comparing against `prior` would refuse every undo that is
    safe and permit exactly the one that is not.

    Refusals raise `ToolError` instead, so a failed call cannot be mistaken for
    a captured snapshot and carries `isError: true` on the MCP wire.
    """
    return json.dumps(
        {"ok": ok, "summary": summary, "prior": prior, "post": post, "target": target},
        sort_keys=True,
    )


def _client() -> httpx.Client:
    """Build an httpx client from the mounted kubeconfig, or raise `ToolError`.

    The kubeconfig is a static one built for this identity (the bundle README's
    write-path section has the assembly steps).
    Parsed here rather than pulling in the full Kubernetes client, so the
    failure modes are ones this file can explain.
    """

    try:
        with open(KUBECONFIG, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ToolError(
            f"no kubeconfig at {KUBECONFIG}: the write credential is not mounted"
        ) from None
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f"could not read the kubeconfig at {KUBECONFIG}: {exc}") from exc

    try:
        cluster = cfg["clusters"][0]["cluster"]
        user = cfg["users"][0]["user"]
        server = cluster["server"]
        token = user["token"]
    except (KeyError, IndexError, TypeError):
        raise ToolError("kubeconfig is missing a cluster server or a user token") from None

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
            raise ToolError(
                f"kubeconfig certificate-authority-data is not a valid base64 PEM: {exc}"
            ) from exc
        ctx = ssl.create_default_context()
        try:
            ctx.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            raise ToolError(
                f"kubeconfig certificate-authority-data is not a usable CA certificate: {exc}"
            ) from exc
        verify = ctx
    elif ca_path:
        verify = ca_path
    elif cluster.get("insecure-skip-tls-verify"):
        # Never silently. A write path that skips verification is a write path
        # that can be pointed at an impostor API server.
        raise ToolError(
            "kubeconfig sets insecure-skip-tls-verify; refusing to write over an "
            "unverified connection"
        )

    return httpx.Client(
        base_url=server,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
        timeout=TIMEOUT,
    )

@mcp.tool(annotations=SCALE)
def scale_deployment(namespace: str, name: str, replicas: int) -> str:
    """Set a Deployment's replica count.

    Equivalent to `kubectl -n <namespace> scale deploy/<name> --replicas=<n>`,
    performed against the `scale` subresource so the credential this runs with
    cannot touch the pod template.

    This is a WRITE and it is gated -- the call pauses the turn for human
    approval before anything happens. Say what you are about to scale, from what
    to what, and why, before calling it.

    REVERSIBLE. The reply carries `prior`, the replica count read immediately
    before the patch. Scaling back to that number restores what was there.

    On success, the reply is a JSON object: `ok`, `summary`, `prior`, `post`,
    `target`. `prior` is what putting this back means; `post` is what the platform
    compares the live Deployment against before allowing that. A refusal is a
    tool error instead, so a failed read never looks like a snapshot.
    """

    namespace = (namespace or "").strip()
    name = (name or "").strip()
    if not namespace or not name:
        raise ToolError("both namespace and name are required")
    if isinstance(replicas, bool) or not isinstance(replicas, int):
        raise ToolError("replicas must be an integer")
    if replicas < 0:
        raise ToolError("replicas must not be negative")
    if replicas > MAX_REPLICAS:
        raise ToolError(
            f"refusing: {replicas} exceeds this connector's ceiling of "
            f"{MAX_REPLICAS}. Raising it is an operator change."
        )

    target = f"{namespace}/{name}"
    if target not in ALLOWLIST:
        permitted = ", ".join(sorted(ALLOWLIST)) or "(none configured)"
        raise ToolError(
            f"refusing: {target} is not in this connector's allowlist. "
            f"Permitted: {permitted}. This is a deliberate ceiling -- widening it "
            "is an operator change, not something to work around."
        )

    client = _client()

    # The scale subresource, not the Deployment. This is the narrow grant.
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale"
    try:
        with client:
            existing = client.get(path)
            if existing.status_code == 404:
                raise ToolError(
                    f"no Deployment {target}. Check the name with the read-only tools."
                )
            if existing.status_code in (401, 403):
                raise ToolError(
                    f"the write identity may not read the scale of {target} "
                    f"({existing.status_code}). It needs get on deployments/scale."
                )
            if existing.status_code >= 400:
                raise ToolError(
                    f"could not read the scale of {target}: {existing.status_code}"
                )
            try:
                prior_replicas = (existing.json().get("spec") or {}).get("replicas")
            except (ValueError, AttributeError):
                prior_replicas = None
            if not isinstance(prior_replicas, int):
                # No trustworthy prior state means no snapshot. Refusing here
                # rather than writing anyway keeps "the action happened" and
                # "we know how to undo it" from drifting apart.
                raise ToolError(
                    f"could not read a replica count for {target}; refusing to "
                    "scale without a prior state to restore"
                )

            # KEYWORD arguments. `httpx.Client.patch` is
            # `patch(url, *, content=..., json=..., headers=...)` -- everything
            # after the URL is keyword only, so passing the body positionally
            # raised TypeError on every call and this verb could never scale
            # anything (#1947). The `except httpx.HTTPError` below does not
            # catch TypeError, so it propagated out of the tool after a human
            # had already approved the write.
            #
            # merge-patch rather than the sibling connector's strategic-merge:
            # `spec.replicas` is a scalar on a flat object, where the two are
            # equivalent, and merge-patch is the one the scale subresource
            # documents.
            patched = client.patch(
                path,
                content=json.dumps({"spec": {"replicas": replicas}}),
                headers={"Content-Type": "application/merge-patch+json"},
            )
            if patched.status_code in (401, 403):
                raise ToolError(
                    f"the write identity may not scale {target} "
                    f"({patched.status_code}). It needs patch on deployments/scale."
                )
            if patched.status_code >= 400:
                raise ToolError(f"scale refused for {target}: {patched.status_code}")
    except httpx.HTTPError as exc:
        raise ToolError(f"could not reach the API server for {target}: {exc}") from exc

    return _reply(
        True,
        f"scaled {target} from {prior_replicas} to {replicas}",
        prior={"spec": {"replicas": prior_replicas}},
        # What this call left. Reported rather than assumed by the reader: the
        # platform must not derive it from `replicas`, because deriving a
        # reversal from the forward call's arguments is the mapping-DSL
        # alternative ADR-0117 rejects, and the connector is the only party that
        # knows the patch was accepted as sent.
        post={"spec": {"replicas": replicas}},
        # `kind` because whatever performs the restore has only this to go on,
        # and is not guaranteed to be this connector.
        target={"kind": "Deployment", "namespace": namespace, "name": name},
    )


def main() -> None:
    if not ALLOWLIST:
        log.warning("K8S_SCALE_ALLOWLIST is empty; every call will be refused")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
