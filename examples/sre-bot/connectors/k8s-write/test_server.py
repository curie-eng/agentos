"""Tests for the write connector.

These cover the claims the bundle README and manifests/write-role.yaml make about this
process being the real constraint. Every one of them is a property that, if it
broke, would leave a write path that still looks like it works: the tool answers,
the restart happens, and the ceiling is somewhere other than where the docs say
it is.

The patch-body test is the load-bearing one. RBAC cannot distinguish "restart"
from "replace the container command" -- they are the same `patch` verb -- so the
separation exists only because this file constructs its own body. A change that
let caller input reach the patch would silently convert a restart tool into an
arbitrary-write tool with an approval gate on it.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import anyio
import httpx
import pytest
import yaml
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session as _connect

_REAL_CA_PEM = (
    b"-----BEGIN CERTIFICATE-----\n"
    b"MIIDBTCCAe2gAwIBAgIUQeMwk8Zbh3Krpfm/jpTLEjqpetIwDQYJKoZIhvcNAQEL\n"
    b"BQAwEjEQMA4GA1UEAwwHdGVzdC1jYTAeFw0yNjA4MTUwNTM0MTNaFw0yNjA4MTYw\n"
    b"NTM0MTNaMBIxEDAOBgNVBAMMB3Rlc3QtY2EwggEiMA0GCSqGSIb3DQEBAQUAA4IB\n"
    b"DwAwggEKAoIBAQDDNfJYrIY+iNN6La+3r2Xd9e+7y6VBX4hRxH24cV8HQOXu9gFs\n"
    b"3VImNsRQTxY/BQlLtZ2/2F+MQgnqoxR8O/eFqVrDTtrs8vXoyPECtUuqYnG3UjtK\n"
    b"ietnfrp6pMygnAP15TOJpMprhSeyZ+QbhvcTBkxAi5rzFTrkWGu1czsSa6/5gSKB\n"
    b"eTC7gjBHdbnxh70Dm7RWzx6J8shXNN0aXPJlIQOL2NTV6LFcIQX6CwyF9gMAjsuC\n"
    b"kWcZqredhn1cZ4MP1HsXJwZkoJQGs6EyQUCmn8+gdp597kqv4aeT5Fr+KPPdbT1k\n"
    b"CRHv/WxXcyRqjHI6r3ZKQNtzIhxYky7pptp/AgMBAAGjUzBRMB0GA1UdDgQWBBQo\n"
    b"gF5ZNNCAWjSeD2e4oYbZPZg47DAfBgNVHSMEGDAWgBQogF5ZNNCAWjSeD2e4oYbZ\n"
    b"PZg47DAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQCje3rual9M\n"
    b"UME0Jh1lFKdijEmWpNAYrmI4u1aDJnIJ7gJhKt0bg1uWd+m2wMNL+z4XlS/Y9eAA\n"
    b"V8u7+CT6SSyrLZCGoMLIO8zzPmdaC8gh+vRt4s8ixuNXMLCzb0tDB54tqNm9/Itf\n"
    b"VVmf+qqq42e6MtKfj/YozGT6XMXTT2u6EcUr+ZrsHTj57UftukJ11eQ3cbUYiFMC\n"
    b"Sqwd+NlfuwyxEnsKzzZnLHYPBLiheEfdzGUx3kOmM36ZaQwksZl2UKKW3Q0kU2B3\n"
    b"Q6xAV6iJRlcecSPg+NXGXsiUtCs4WSfgLsWT5IlxI0oxezNtDjJ0xyttcrl3GNTu\n"
    b"6gjy/AyJp++o\n"
    b"-----END CERTIFICATE-----\n"
)

# Load server.py BY PATH under a unique module name, rather than putting this
# directory on sys.path and importing `server`.
#
# Both connectors in this bundle ship a file called server.py. With the sys.path
# approach they collide: whichever suite imports first wins the name `server`,
# the other silently gets the wrong module, and every assertion in it fails on a
# missing attribute. That only shows up when both suites run in ONE pytest
# invocation, so it passes per-directory and breaks the moment anyone points
# pytest at examples/sre-bot/connectors.
_MODULE_NAME = "sre_bot_k8s_write_server"
_SERVER_PY = Path(__file__).parent / "server.py"

GOOD_KUBECONFIG = {
    "clusters": [{"cluster": {"server": "https://k8s.example:6443"}}],
    "users": [{"user": {"token": "write-token"}}],
}


def _load(tmp_path, kubeconfig=GOOD_KUBECONFIG, allowlist="public/api,public/agents"):
    """Import server.py with a specific environment and kubeconfig on disk.

    Module-level config is read at import time, so each case needs a fresh
    import rather than monkeypatching the constants afterwards.
    """
    cfg = tmp_path / "kubeconfig"
    if kubeconfig is not None:
        cfg.write_text(yaml.safe_dump(kubeconfig), encoding="utf-8")
    for key in ("K8S_WRITE_ALLOWLIST", "KUBECONFIG_PATH", "K8S_TIMEOUT_SECONDS"):
        os.environ.pop(key, None)
    os.environ["KUBECONFIG_PATH"] = str(cfg)
    if allowlist is not None:
        os.environ["K8S_WRITE_ALLOWLIST"] = allowlist

    sys.modules.pop(_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module can find itself if it ever needs to;
    # under the unique name only, never as `server`.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    """Stands in for httpx.Client, recording what the server sent."""

    def __init__(self, get_status=200, patch_status=200, seen=None):
        self.get_status = get_status
        self.patch_status = patch_status
        self.seen = seen if seen is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path):
        self.seen["get_path"] = path
        return httpx.Response(self.get_status, json={"kind": "Deployment"})

    def patch(self, path, content=None, headers=None):
        self.seen["patch_path"] = path
        self.seen["body"] = content
        self.seen["headers"] = headers
        return httpx.Response(self.patch_status, json={})


# --------------------------------------------------------------------------- #
# The allowlist. RBAC also scopes this, but a ceiling stated in one place only
# is a ceiling that moves when someone edits the other place.
# --------------------------------------------------------------------------- #
def test_allowlisted_target_is_patched(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen=seen))
    out = srv.restart_deployment("public", "api")
    assert "restart triggered" in out
    assert seen["patch_path"] == "/apis/apps/v1/namespaces/public/deployments/api"


@pytest.mark.parametrize(
    # A right name in the wrong namespace, a right namespace with the wrong
    # name, and a plausible platform namespace. All three must be refused before
    # a client is ever built.
    "ns,name", [("public", "not-listed"), ("platform", "api"), ("default", "api")]
)
def test_target_outside_the_allowlist_never_reaches_the_api(tmp_path, monkeypatch, ns, name):
    srv = _load(tmp_path)

    def explode():
        raise AssertionError("built a client for a target outside the allowlist")

    monkeypatch.setattr(srv, "_client", explode)
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment(ns, name)
    assert "refusing" in str(excinfo.value) and "allowlist" in str(excinfo.value)


def test_empty_allowlist_refuses_to_start(tmp_path):
    # A write connector that permits nothing looks healthy to Kubernetes and
    # refuses every call, which reads as a broken bot rather than missing config.
    srv = _load(tmp_path, allowlist="")
    assert srv.main() == 1


# --------------------------------------------------------------------------- #
# The patch body. This is the whole separation between "restart" and "replace
# what runs", because RBAC cannot express it.
# --------------------------------------------------------------------------- #
def test_patch_body_only_ever_sets_the_restart_annotation(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen=seen))
    srv.restart_deployment("public", "api")

    body = json.loads(seen["body"])
    annotations = body["spec"]["template"]["metadata"]["annotations"]
    assert list(annotations) == ["kubectl.kubernetes.io/restartedAt"]
    # Nothing that could change WHAT runs.
    flat = json.dumps(body)
    for forbidden in ("image", "command", "args", "env", "replicas", "serviceAccount"):
        assert forbidden not in flat
    assert seen["headers"]["Content-Type"] == "application/strategic-merge-patch+json"


def test_tool_signature_exposes_no_way_to_shape_the_patch(tmp_path):
    # The argument list IS the security boundary: a caller cannot supply what the
    # tool cannot accept. If a parameter is ever added here, it must not reach
    # the patch body -- and the test above is what proves that.
    srv = _load(tmp_path)
    import inspect

    params = list(inspect.signature(srv.restart_deployment).parameters)
    assert params == ["namespace", "name"]


# --------------------------------------------------------------------------- #
# Credential handling
# --------------------------------------------------------------------------- #
def test_insecure_skip_tls_verify_is_refused(tmp_path):
    srv = _load(
        tmp_path,
        kubeconfig={
            "clusters": [{"cluster": {"server": "https://k8s.example:6443",
                                      "insecure-skip-tls-verify": True}}],
            "users": [{"user": {"token": "t"}}],
        },
    )
    with pytest.raises(ToolError) as excinfo:
        srv._client()
    assert "insecure-skip-tls-verify" in str(excinfo.value)


def test_missing_kubeconfig_raises_a_sentence_not_a_traceback(tmp_path):
    # A ToolError still reaches the model as its message behind FastMCP's fixed
    # `Error executing tool ...: ` prefix and nothing else -- never a traceback
    # -- so naming the missing mount survives the change from returning to
    # raising. (The wire test below pins that prefix exactly.)
    srv = _load(tmp_path, kubeconfig=None)
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment("public", "api")
    assert "not mounted" in str(excinfo.value)


@pytest.mark.parametrize("failure", ["transport", "http-error"])
def test_token_never_appears_in_an_error_message(tmp_path, monkeypatch, failure):
    # str(exc) on an httpx error carries the URL, and the URL is built from the
    # kubeconfig -- so a message that interpolates the exception is one edit away
    # from carrying the bearer token into a Slack channel. Raising rather than
    # returning does not change that: the ToolError's text is what the model
    # sees, so it is the text that must not carry the credential.
    srv = _load(tmp_path)

    class Failing(_FakeClient):
        def get(self, path):
            if failure == "transport":
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"kind": "Deployment"})

        def patch(self, path, content=None, headers=None):
            return httpx.Response(500, text="internal error")

    monkeypatch.setattr(srv, "_client", lambda: Failing())
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment("public", "api")
    message = str(excinfo.value)
    assert "write-token" not in message
    if failure == "transport":
        # str(exc) on a transport error carries the URL, which is built from the
        # kubeconfig. It must not carry the credential with it.
        assert "could not reach the API server" in message
    else:
        assert "the restart was rejected: 500" in message


# --------------------------------------------------------------------------- #
# Failure messages. A write that MIGHT have happened is the dangerous case.
# --------------------------------------------------------------------------- #
def test_timeout_says_do_not_retry(tmp_path, monkeypatch):
    srv = _load(tmp_path)

    class Timeouts(_FakeClient):
        def get(self, path):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", lambda: Timeouts())
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment("public", "api")
    message = str(excinfo.value)
    assert "Do not call this again" in message
    assert "MAY have" in message


@pytest.mark.parametrize("status", [401, 403])
def test_permission_errors_say_they_will_not_fix_themselves(tmp_path, monkeypatch, status):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(get_status=status))
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment("public", "api")
    assert "not fix itself" in str(excinfo.value)


def test_missing_deployment_is_reported_plainly(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(get_status=404))
    with pytest.raises(ToolError) as excinfo:
        srv.restart_deployment("public", "api")
    assert "no Deployment public/api" in str(excinfo.value)


def test_success_message_does_not_claim_the_rollout_finished(tmp_path, monkeypatch):
    # The bot must verify with reads. A message that sounds like completion is
    # how "I restarted it" gets reported when nothing came up healthy.
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient())
    out = srv.restart_deployment("public", "api")
    assert "not that the rollout completed" in out


# --------------------------------------------------------------------------- #
# The wire. Everything above calls the tool function directly, and no assertion
# at that level can see the field this connector is actually judged by. FastMCP
# marks a result `isError: true` only when the call RAISED; a refusal that is
# `return`ed is a string like any other, so it goes out as `isError: false` and
# is indistinguishable from a completed restart to anything reading the protocol
# rather than the prose -- an eval grader, an audit ledger, a retry policy.
#
# So this one goes through the real MCP request path, in process, and asserts
# the flag on the CallToolResult.
# --------------------------------------------------------------------------- #
def _call_tool(srv, name, args):
    """Call one tool through the real MCP request path and return the CallToolResult."""

    async def go():
        async with _connect(srv.mcp._mcp_server) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def test_a_refusal_and_a_restart_carry_different_is_error_flags(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    # A client that would succeed, so the only thing separating the two calls is
    # the allowlist -- not a broken fake.
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient())

    refused = _call_tool(srv, "restart_deployment", {"namespace": "platform", "name": "api"})
    assert refused.isError is True
    # The prose survives the trip, minus a fixed SDK prefix: FastMCP puts our
    # message text on the wire and adds no traceback, so raising costs nothing
    # the sentence was doing.
    refusal_text = refused.content[0].text
    assert "refusing" in refusal_text
    assert "allowlist" in refusal_text
    assert "deliberate ceiling" in refusal_text

    # The SDK prefixes the message; OBSERVED against the pinned mcp==1.28.1,
    # which builds it at mcp/server/fastmcp/tools/base.py:117
    # (`raise ToolError(f"Error executing tool {self.name}: {e}") from e`) and
    # puts str(e) in as the only text content. Pinned rather than
    # substring-matched so a version bump that reshapes or drops the prefix
    # fails here instead of quietly changing what an operator reads in Slack.
    prefix = "Error executing tool restart_deployment: "
    assert refusal_text.startswith(prefix)
    # ...and what follows it is our sentence verbatim, with the permitted list
    # built from the module's own allowlist. Rewording the refusal fails here
    # too, not just changing the prefix.
    permitted = ", ".join(sorted(srv.ALLOWLIST))
    assert refusal_text[len(prefix):] == (
        "refusing: platform/api is not in this connector's allowlist. "
        f"Permitted: {permitted}. This is a deliberate ceiling -- widening it "
        "is an operator change, not something to work around."
    )

    done = _call_tool(srv, "restart_deployment", {"namespace": "public", "name": "api"})
    assert done.isError is False
    assert "restart triggered" in done.content[0].text
    # The contrast in the OTHER direction: the success path carries no prefix at
    # all, so the two results differ in text shape as well as in the flag.
    assert not done.content[0].text.startswith("Error executing tool")
    assert done.content[0].text.startswith("restart triggered for public/api at ")

    # THE contrast, stated outright: this is the whole property. When both of
    # these were returned strings the two results were the same shape, and a
    # program counting successful restarts counted the refusal as one.
    assert refused.isError != done.isError


# --------------------------------------------------------------------------- #
# Annotations
# --------------------------------------------------------------------------- #
def test_tool_is_annotated_as_a_non_idempotent_write(tmp_path):
    srv = _load(tmp_path)
    assert srv.WRITE.readOnlyHint is False
    # Each call starts a NEW rollout, so a client that retries on timeout rolls
    # twice. This annotation is what tells it not to.
    assert srv.WRITE.idempotentHint is False


def test_inline_ca_data_is_materialised_for_verification(tmp_path, monkeypatch):
    # The bug that made the first real gated write fail AFTER approval: only the
    # file-path CA form was handled, so an `-data` kubeconfig left verify=True,
    # httpx checked EKS against the system trust store, and the call died with
    # CERTIFICATE_VERIFY_FAILED -- which reads like a cluster problem.
    import base64 as _b64
    pem = _REAL_CA_PEM
    srv = _load(tmp_path, kubeconfig={
        "clusters": [{"cluster": {"server": "https://k8s.example:6443",
                                  "certificate-authority-data": _b64.b64encode(pem).decode()}}],
        "users": [{"user": {"token": "write-token"}}],
    })
    seen = {}
    monkeypatch.setattr(srv.httpx, "Client", lambda **kw: seen.update(kw) or _FakeClient())
    srv._client()
    # An SSLContext, NOT a path: the connector runs on a read-only rootfs with no
    # writable scratch, so writing the CA to a temp file raises "no usable
    # temporary directory" -- which surfaced as a tool-side failure after a human
    # had already approved the write.
    import ssl as _ssl
    assert isinstance(seen["verify"], _ssl.SSLContext), "verify must be an in-memory SSLContext"
    assert seen["verify"].get_ca_certs(), "the CA was not loaded"
