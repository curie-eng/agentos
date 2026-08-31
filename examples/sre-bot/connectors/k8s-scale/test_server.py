"""Tests for the scale connector.

The load-bearing one is `test_a_failed_read_refuses_instead_of_scaling`. This
tool's reason to exist is that its reply can be trusted as a snapshot, so a path
where the write lands but the prior state does not is worse than no write at all:
the platform would hold an action it believes happened and cannot undo.
"""

import importlib.util
import inspect
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

_MODULE_NAME = "sre_bot_k8s_scale_server"
_SERVER_PY = Path(__file__).parent / "server.py"

GOOD_KUBECONFIG = {
    "clusters": [{"cluster": {"server": "https://k8s.example:6443"}}],
    "users": [{"user": {"token": "scale-token"}}],
}


def _load(tmp_path, kubeconfig=GOOD_KUBECONFIG, allowlist="public/api", ceiling="50"):
    cfg = tmp_path / "kubeconfig"
    cfg.write_text(yaml.safe_dump(kubeconfig), encoding="utf-8")
    os.environ["KUBECONFIG_PATH"] = str(cfg)
    os.environ["K8S_SCALE_ALLOWLIST"] = allowlist
    os.environ["K8S_SCALE_MAX_REPLICAS"] = ceiling
    sys.modules.pop(_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    """Records what the tool asked the API server to do."""

    def __init__(self, seen, get=(200, {"spec": {"replicas": 3}}), patch=(200, {})):
        self.seen = seen
        self._get = get
        self._patch = patch

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # These signatures MIRROR httpx.Client deliberately, keyword-only marker
    # included. The first version of this fake took the patch body positionally,
    # so it accepted a call the real client rejects with TypeError -- the tool
    # could never scale anything and every test here passed (#1947). A fake
    # looser than the thing it stands in for tests the fake.
    def get(self, path, *, params=None):
        self.seen["get_path"] = path
        return _Response(*self._get)

    def patch(self, path, *, content=None, headers=None, json=None):
        # Records only; the tests decode. A recorder that also interprets is a
        # second place for the expectation to live.
        self.seen["patch_path"] = path
        self.seen["patch_headers"] = headers
        self.seen["patch_content"] = content
        self.seen["patch_json"] = json
        return _Response(*self._patch)


def test_the_reply_carries_the_replica_count_read_before_the_patch(tmp_path, monkeypatch):
    """Without prior state the action is not undoable, which is the whole point."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    result = json.loads(srv.scale_deployment("public", "api", 10))
    assert result["ok"] is True
    assert result["prior"] == {"spec": {"replicas": 3}}
    assert result["target"] == {"kind": "Deployment", "namespace": "public", "name": "api"}
    assert "from 3 to 10" in result["summary"]


def test_it_writes_through_the_scale_subresource(tmp_path, monkeypatch):
    """The narrow grant is the security argument; patching the Deployment is not it."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    srv.scale_deployment("public", "api", 10)
    assert seen["get_path"] == "/apis/apps/v1/namespaces/public/deployments/api/scale"
    assert seen["patch_path"] == "/apis/apps/v1/namespaces/public/deployments/api/scale"


def test_the_patch_body_only_ever_sets_replicas(tmp_path, monkeypatch):
    """Caller input reaches exactly one integer and nothing else."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    srv.scale_deployment("public", "api", 7)
    assert json.loads(seen["patch_content"]) == {"spec": {"replicas": 7}}
    assert seen["patch_headers"] == {"Content-Type": "application/merge-patch+json"}


def test_a_failed_read_refuses_instead_of_scaling(tmp_path, monkeypatch):
    """No trustworthy prior state means no write: an un-undoable action is worse."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, get=(403, {})))
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", 10)
    assert "get on deployments/scale" in str(excinfo.value)
    assert "patch_content" not in seen


def test_a_read_with_no_replica_count_refuses(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, get=(200, {"spec": {}})))
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", 10)
    assert "prior state" in str(excinfo.value)
    assert "patch_content" not in seen


@pytest.mark.parametrize("ns,name", [("public", "not-listed"), ("platform", "api")])
def test_a_target_outside_the_allowlist_never_reaches_the_api(tmp_path, monkeypatch, ns, name):
    srv = _load(tmp_path)

    def explode():
        raise AssertionError("a client must never be built for a refused target")

    monkeypatch.setattr(srv, "_client", explode)
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment(ns, name, 10)
    assert "allowlist" in str(excinfo.value)


def test_the_ceiling_refuses_before_a_client_is_built(tmp_path, monkeypatch):
    """Scale to ten thousand is a denial of service with an approval on it."""
    srv = _load(tmp_path, ceiling="50")

    def explode():
        raise AssertionError("a client must never be built for a refused target")

    monkeypatch.setattr(srv, "_client", explode)
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", 10_000)
    assert "ceiling" in str(excinfo.value)


@pytest.mark.parametrize("bad", [True, "3", 3.5, None])
def test_a_non_integer_replica_count_is_refused(tmp_path, monkeypatch, bad):
    """bool is an int in Python; a caller passing True must not scale to 1."""
    srv = _load(tmp_path)

    def explode():
        raise AssertionError("a client must never be built for a refused target")

    monkeypatch.setattr(srv, "_client", explode)
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", bad)
    assert "integer" in str(excinfo.value)


def test_insecure_skip_tls_verify_is_refused(tmp_path):
    srv = _load(
        tmp_path,
        kubeconfig={
            "clusters": [
                {
                    "cluster": {
                        "server": "https://k8s.example:6443",
                        "insecure-skip-tls-verify": True,
                    }
                }
            ],
            "users": [{"user": {"token": "scale-token"}}],
        },
    )
    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", 3)
    assert "insecure-skip-tls-verify" in str(excinfo.value)


# --- What the reply has to carry for the platform to rule on an undo ----------


def test_a_successful_scale_reports_what_it_left(tmp_path, monkeypatch):
    """`prior` alone is not enough, and the difference is not cosmetic.

    ADR-0117 decision 4 refuses a restore when the resource no longer looks like
    what the action LEFT. Comparing a live resource against `prior` instead would
    refuse every undo that is safe and permit exactly the one that is not, so the
    two are reported separately -- and differ here, because a fixture where they
    matched would let that confusion pass.
    """
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))

    reply = json.loads(srv.scale_deployment("public", "api", 10))

    assert reply["prior"] == {"spec": {"replicas": 3}}
    assert reply["post"] == {"spec": {"replicas": 10}}


def test_the_target_names_its_kind(tmp_path, monkeypatch):
    """Whatever performs the restore has only this to go on.

    A namespace and a name do not say WHAT to scale, and the executor is not
    guaranteed to be the connector that wrote it.
    """
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))

    reply = json.loads(srv.scale_deployment("public", "api", 10))

    assert reply["target"] == {"kind": "Deployment", "namespace": "public", "name": "api"}


def test_a_missing_deployment_is_an_error_without_a_write(tmp_path, monkeypatch):
    """A missing target is a refusal, and it must never reach the patch."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, get=(404, {})))

    with pytest.raises(ToolError) as excinfo:
        srv.scale_deployment("public", "api", 10)

    assert "no Deployment public/api" in str(excinfo.value)
    assert "patch_content" not in seen


# --- The MCP wire distinguishes a refusal from a completed scale -------------


def _call_tool(srv, name, args):
    """Call one tool through the real MCP path and return its CallToolResult."""

    async def go():
        async with _connect(srv.mcp._mcp_server) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def test_a_refusal_and_a_scale_carry_different_is_error_flags(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))

    refused = _call_tool(
        srv,
        "scale_deployment",
        {"namespace": "platform", "name": "api", "replicas": 10},
    )
    assert refused.isError is True
    refusal_text = refused.content[0].text

    # OBSERVED against the pinned mcp==1.28.1. FastMCP builds this prefix at
    # mcp/server/fastmcp/tools/base.py:117
    # (`raise ToolError(f"Error executing tool {self.name}: {e}") from e`) and
    # puts str(e) in the result as its only text content. Pin the whole string so
    # an SDK change cannot silently reshape what operators and agents read.
    assert refusal_text == (
        "Error executing tool scale_deployment: refusing: platform/api is not "
        "in this connector's allowlist. Permitted: public/api. This is a "
        "deliberate ceiling -- widening it is an operator change, not something "
        "to work around."
    )

    scaled = _call_tool(
        srv,
        "scale_deployment",
        {"namespace": "public", "name": "api", "replicas": 10},
    )
    assert scaled.isError is False
    assert not scaled.content[0].text.startswith("Error executing tool")
    payload = json.loads(scaled.content[0].text)
    assert payload["ok"] is True
    assert payload["prior"] == {"spec": {"replicas": 3}}
    assert payload["post"] == {"spec": {"replicas": 10}}
    assert payload["target"] == {
        "kind": "Deployment",
        "namespace": "public",
        "name": "api",
    }
    assert refused.isError != scaled.isError


def test_streamable_http_is_mounted_at_curie_connector_path(tmp_path):
    srv = _load(tmp_path)
    assert [route.path for route in srv.mcp.streamable_http_app().routes] == ["/mcp"]


def test_the_fake_client_cannot_accept_a_call_the_real_one_rejects():
    """The defect in #1947 survived a full test suite because of this.

    `scale_deployment` passed the patch body positionally. Every test here
    passed, because `_FakeClient.patch` accepted it positionally too -- while the
    real `httpx.Client.patch` takes everything after the URL keyword-only and
    raises `TypeError`. The suite was testing the fake.

    So the fake's signature is pinned against the real client's: every parameter
    it accepts after the URL must exist on `httpx.Client` and be keyword-only
    there and here. Loosening the fake fails this test instead of silently
    re-opening the hole.
    """

    import httpx

    for method in ("get", "patch"):
        real = inspect.signature(getattr(httpx.Client, method)).parameters
        fake = inspect.signature(getattr(_FakeClient, method)).parameters
        # [0] is self, [1] is the URL; both are positional in httpx too.
        for name, param in list(fake.items())[2:]:
            assert name in real, f"_FakeClient.{method} accepts {name!r}, httpx does not"
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"_FakeClient.{method}'s {name!r} must be keyword-only, "
                f"because httpx's is -- see this test's docstring"
            )
            assert real[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_it_scales_through_a_real_httpx_client(tmp_path, monkeypatch):
    """The one test in this file that would have caught #1947 on its own.

    Every other test here substitutes the client. This one keeps the REAL
    `httpx.Client` -- its real signatures, its real request encoding -- and
    replaces only the transport underneath it. The positional-body call raises
    `TypeError` here exactly as it did in the cluster, and no fake stands between
    the tool and that fact.

    Worth the extra machinery precisely because the cheaper tests all passed
    while the verb could never scale anything.
    """

    srv = _load(tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.method] = request
        if request.method == "GET":
            return httpx.Response(200, json={"spec": {"replicas": 3}})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        srv,
        "_client",
        lambda: httpx.Client(
            base_url="https://k8s.example:6443",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = json.loads(srv.scale_deployment("public", "api", 9))

    assert result["ok"] is True, result["summary"]
    assert result["prior"] == {"spec": {"replicas": 3}}
    patch = seen["PATCH"]
    assert patch.url.path == "/apis/apps/v1/namespaces/public/deployments/api/scale"
    assert json.loads(patch.content) == {"spec": {"replicas": 9}}
    assert patch.headers["content-type"] == "application/merge-patch+json"
