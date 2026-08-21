"""Tests for the Tempo connector.

These cover the parts that fail SILENTLY in production. A broken URL, a
swallowed error, or an uncapped limit all leave a connector that starts, passes
a health check, and returns something plausible -- so none of them show up as a
crash. Everything here is a property that would otherwise only be noticed by
someone reading a wrong answer in Slack.
"""

import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

# Load server.py BY PATH under a unique module name, rather than putting this
# directory on sys.path and importing `server`.
#
# Both connectors in this bundle ship a file called server.py. With the sys.path
# approach they collide: whichever suite imports first wins the name `server`,
# the other silently gets the wrong module, and every assertion in it fails on a
# missing attribute. That only shows up when both suites run in ONE pytest
# invocation, so it passes per-directory and breaks the moment anyone points
# pytest at examples/sre-bot/connectors.
_MODULE_NAME = "sre_bot_tempo_server"
_SERVER_PY = Path(__file__).parent / "server.py"


def _load(**env):
    """Import server.py with a specific environment.

    Module-level config is read at import time, so each case needs a fresh
    import rather than monkeypatching the constants afterwards.
    """

    base = {
        "GRAFANA_URL": "https://grafana.example.com",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN": "glsa_test",
    }
    base.update(env)
    for k in ("GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN"):
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in base.items() if v is not None})

    sys.modules.pop(_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module can find itself if it ever needs to;
    # under the unique name only, never as `server`.
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _with_one_tempo(monkeypatch, srv, proxy_get, uid="tempo-prod"):
    """Route Grafana datasource discovery, then delegate the Tempo proxy call."""

    def fake_get(url, *args, **kwargs):
        if url == "https://grafana.example.com/api/datasources":
            return httpx.Response(200, json=[{"name": "Tempo", "uid": uid}])
        return proxy_get(url, *args, **kwargs)

    monkeypatch.setattr(srv.httpx, "get", fake_get)


# --------------------------------------------------------------------------- #
# The URL. Getting this wrong yields 404s that look like "no traces".
# --------------------------------------------------------------------------- #
def test_discovers_tempo_then_returns_a_curie_span_through_grafanas_proxy(monkeypatch):
    srv = _load()
    seen = []
    payload = {
        "batches": [
            {
                "resource": {"service.name": "curie-worker"},
                "spans": [{"name": "curie.run", "traceId": "curie-trace"}],
            }
        ]
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append((url, params, headers))
        if url == "https://grafana.example.com/api/datasources":
            return httpx.Response(
                200,
                json=[
                    {"name": "Loki", "uid": "loki"},
                    {"name": "Tempo", "uid": "tempo/prod arm64"},
                ],
            )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(srv.httpx, "get", fake_get)
    assert srv.get_trace("curie/trace id") == payload
    assert [call[0] for call in seen] == [
        "https://grafana.example.com/api/datasources",
        (
            "https://grafana.example.com/api/datasources/proxy/uid/"
            "tempo%2Fprod%20arm64/api/traces/curie%2Ftrace%20id"
        ),
    ]
    assert all(headers["Authorization"] == "Bearer glsa_test" for _, _, headers in seen)
    assert all("token" not in str(params).lower() for _, params, _ in seen)


@pytest.mark.parametrize(
    "datasources",
    [
        [],
        [
            {"name": "Tempo", "uid": "tempo-a"},
            {"name": "Tempo", "uid": "tempo-b"},
        ],
    ],
)
def test_zero_or_duplicate_tempo_datasources_refuse_before_proxy(monkeypatch, datasources):
    srv = _load()
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        assert url == "https://grafana.example.com/api/datasources"
        return httpx.Response(200, json=datasources)

    monkeypatch.setattr(srv.httpx, "get", fake_get)
    result = srv.list_trace_tags()
    assert isinstance(result, str)
    assert "exactly one" in result
    assert "Tempo" in result
    assert seen == ["https://grafana.example.com/api/datasources"]


# --------------------------------------------------------------------------- #
# Limits. An uncapped trace fetch is a context-window denial of service.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("asked,expected", [(99, 50), (0, 1), (-5, 1), (10, 10), ("7", 7)])
def test_limit_is_clamped(monkeypatch, asked, expected):
    srv = _load()
    seen = {}

    def fake_get(url, params=None, **kw):
        seen["p"] = params
        return httpx.Response(200, json={})

    _with_one_tempo(monkeypatch, srv, fake_get)
    srv.search_traces("{}", limit=asked)
    assert seen["p"]["limit"] == expected


# --------------------------------------------------------------------------- #
# Errors. Every one is returned as a SENTENCE, because a raised exception
# reaches the model as a stack trace -- which it pastes into Slack or retries.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,must_contain",
    [
        (403, "refused"),
        (401, "refused"),
        (404, "Tempo"),
        (500, "500"),
    ],
)
def test_http_errors_become_readable_strings(monkeypatch, status, must_contain):
    srv = _load()
    _with_one_tempo(
        monkeypatch,
        srv,
        lambda *a, **kw: httpx.Response(status, text="boom"),
    )
    out = srv.search_traces("{}")
    assert isinstance(out, str)
    assert must_contain in out


# --------------------------------------------------------------------------- #
# The response-size ceiling. `limit` bounds how many traces a search returns and
# says nothing about how big one is, so `get_trace` is the uncapped path: one
# call, one trace, potentially megabytes. The connector runs under a 256Mi limit
# and pastes what it returns into a chat channel, so an uncapped body is either
# an OOMKill that looks like a random restart, or the model's whole context
# spent on one trace.
# --------------------------------------------------------------------------- #


def test_oversized_body_is_refused_by_content_length(monkeypatch):
    # Declared size only -- the body is never read, which is the point: this
    # path must refuse without materialising the megabytes it is refusing.
    srv = _load()
    oversized = str(srv.MAX_RESPONSE_BYTES + 1)
    _with_one_tempo(
        monkeypatch,
        srv,
        lambda *a, **kw: httpx.Response(
            200, json={"ok": True}, headers={"content-length": oversized}
        ),
    )
    out = srv.get_trace("abc123")
    assert isinstance(out, str)
    assert "cap" in out
    # Actionable, and explicitly not retryable -- otherwise the agent tries
    # three times and reports a timeout.
    assert "Narrow the query" in out
    assert "refused again" in out


def test_oversized_body_is_refused_when_content_length_is_absent(monkeypatch):
    # A very large trace comes back chunked, so there is no content-length to
    # check. Falling back to the received body's size is what closes that hole.
    srv = _load()
    big = {"spans": ["x" * 1000] * ((srv.MAX_RESPONSE_BYTES // 1000) + 20)}
    resp = httpx.Response(200, json=big)
    del resp.headers["content-length"]
    _with_one_tempo(monkeypatch, srv, lambda *a, **kw: resp)
    out = srv.get_trace("abc123")
    assert isinstance(out, str)
    assert "cap" in out


def test_a_normal_sized_trace_is_still_returned(monkeypatch):
    # The bound must not swallow the ordinary case; a grader that only checks
    # the refusal would pass with the cap set to zero.
    srv = _load()
    payload = {"batches": [{"spans": [{"name": "GET /health"}]}]}
    _with_one_tempo(monkeypatch, srv, lambda *a, **kw: httpx.Response(200, json=payload))
    assert srv.get_trace("abc123") == payload


def test_search_results_are_capped_too(monkeypatch):
    # The bound lives in _proxy rather than in get_trace, so every tool is
    # covered -- a wide TraceQL search can also return more than the ceiling.
    srv = _load()
    oversized = str(srv.MAX_RESPONSE_BYTES + 1)
    _with_one_tempo(
        monkeypatch,
        srv,
        lambda *a, **kw: httpx.Response(
            200, json={"traces": []}, headers={"content-length": oversized}
        ),
    )
    assert "cap" in srv.search_traces("{}")


def test_auth_failure_says_it_will_not_fix_itself(monkeypatch):
    # Without this the agent retries a 403 three times and reports a timeout.
    srv = _load()
    _with_one_tempo(monkeypatch, srv, lambda *a, **kw: httpx.Response(403))
    assert "not fix itself" in srv.search_traces("{}")


def test_timeout_is_not_an_exception(monkeypatch):
    srv = _load()

    def boom(*a, **kw):
        raise httpx.TimeoutException("too slow")

    _with_one_tempo(monkeypatch, srv, boom)
    out = srv.search_traces("{}")
    assert isinstance(out, str) and "did not respond" in out


def test_transport_error_does_not_leak_the_token(monkeypatch):
    srv = _load()

    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    _with_one_tempo(monkeypatch, srv, boom)
    out = srv.search_traces("{}")
    assert "glsa_test" not in out


# --------------------------------------------------------------------------- #
# Guard rails on input
# --------------------------------------------------------------------------- #
def test_empty_trace_id_is_rejected_before_a_request(monkeypatch):
    srv = _load()

    def explode(*a, **kw):
        raise AssertionError("should not have called Grafana")

    monkeypatch.setattr(srv.httpx, "get", explode)
    assert "required" in srv.get_trace("   ")
    assert "required" in srv.list_trace_tag_values("")


def test_every_tool_is_annotated_read_only():
    # readOnlyHint is not the boundary -- the Viewer token is -- but a
    # write-shaped tool should never reach a prompt-injectable agent's context.
    srv = _load()
    assert srv.READ_ONLY.readOnlyHint is True
    assert srv.READ_ONLY.destructiveHint is False


def test_missing_config_refuses_rather_than_answering_nonsense(monkeypatch):
    # A connector that answers "not configured" to every call looks healthy to
    # Kubernetes and is useless to the agent, so main() exits non-zero.
    srv = _load(GRAFANA_URL="", GRAFANA_SERVICE_ACCOUNT_TOKEN="")
    assert srv.main() == 1
