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

import httpx
import pytest
import yaml

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
    out = srv.restart_deployment(ns, name)
    assert "refusing" in out and "allowlist" in out


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
    out = srv._client()
    assert isinstance(out, str) and "insecure-skip-tls-verify" in out


def test_missing_kubeconfig_is_a_sentence_not_a_traceback(tmp_path):
    srv = _load(tmp_path, kubeconfig=None)
    out = srv.restart_deployment("public", "api")
    assert isinstance(out, str) and "not mounted" in out


@pytest.mark.parametrize("failure", ["transport", "http-error"])
def test_token_never_appears_in_a_returned_message(tmp_path, monkeypatch, failure):
    # str(exc) on an httpx error carries the URL, and the URL is built from the
    # kubeconfig -- so a message that interpolates the exception is one edit away
    # from carrying the bearer token into a Slack channel.
    srv = _load(tmp_path)

    class Failing(_FakeClient):
        def get(self, path):
            if failure == "transport":
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"kind": "Deployment"})

        def patch(self, path, content=None, headers=None):
            return httpx.Response(500, text="internal error")

    monkeypatch.setattr(srv, "_client", lambda: Failing())
    out = srv.restart_deployment("public", "api")
    assert isinstance(out, str)
    assert "write-token" not in out
    if failure == "transport":
        # str(exc) on a transport error carries the URL, which is built from the
        # kubeconfig. It must not carry the credential with it.
        assert "could not reach the API server" in out
    else:
        assert "the restart was rejected: 500" in out


# --------------------------------------------------------------------------- #
# Failure messages. A write that MIGHT have happened is the dangerous case.
# --------------------------------------------------------------------------- #
def test_timeout_says_do_not_retry(tmp_path, monkeypatch):
    srv = _load(tmp_path)

    class Timeouts(_FakeClient):
        def get(self, path):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr(srv, "_client", lambda: Timeouts())
    out = srv.restart_deployment("public", "api")
    assert "Do not call this again" in out
    assert "MAY have" in out


@pytest.mark.parametrize("status", [401, 403])
def test_permission_errors_say_they_will_not_fix_themselves(tmp_path, monkeypatch, status):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(get_status=status))
    out = srv.restart_deployment("public", "api")
    assert "not fix itself" in out


def test_missing_deployment_is_reported_plainly(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(get_status=404))
    out = srv.restart_deployment("public", "api")
    assert "no Deployment public/api" in out


def test_success_message_does_not_claim_the_rollout_finished(tmp_path, monkeypatch):
    # The bot must verify with reads. A message that sounds like completion is
    # how "I restarted it" gets reported when nothing came up healthy.
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient())
    out = srv.restart_deployment("public", "api")
    assert "not that the rollout completed" in out


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
