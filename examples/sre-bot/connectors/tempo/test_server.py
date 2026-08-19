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
    for k in ("GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN", "TEMPO_DATASOURCE_UID"):
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


# --------------------------------------------------------------------------- #
# The URL. Getting this wrong yields 404s that look like "no traces".
# --------------------------------------------------------------------------- #
def test_requests_go_through_grafanas_datasource_proxy(monkeypatch):
    srv = _load()
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["auth"] = headers.get("Authorization")
        return httpx.Response(200, json={"traces": []})

    monkeypatch.setattr(srv.httpx, "get", fake_get)
    srv.search_traces("{duration > 500ms}")
    assert seen["url"] == (
        "https://grafana.example.com/api/datasources/proxy/uid/__Tempo__/api/search"
    )
    # The token goes to Grafana and nowhere else. It must never be a query
    # parameter, where it would land in Grafana's access log.
    assert seen["auth"] == "Bearer glsa_test"
    assert "token" not in str(seen["params"]).lower()


def test_datasource_uid_is_configurable(monkeypatch):
    srv = _load(TEMPO_DATASOURCE_UID="tempo-prod")
    seen = {}

    # NOT `seen.setdefault(...) or Response(...)`: setdefault returns the
    # stored value, which is truthy, so `or` short-circuits and the fake
    # returns a dict where a Response belongs.
    def fake_get(url, **kw):
        seen["url"] = url
        return httpx.Response(200, json={})

    monkeypatch.setattr(srv.httpx, "get", fake_get)
    srv.list_trace_tags()
    assert "/uid/tempo-prod/" in seen["url"]


# --------------------------------------------------------------------------- #
# Limits. An uncapped trace fetch is a context-window denial of service.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "asked,expected", [(99, 50), (0, 1), (-5, 1), (10, 10), ("7", 7)]
)
def test_limit_is_clamped(monkeypatch, asked, expected):
    srv = _load()
    seen = {}

    def fake_get(url, params=None, **kw):
        seen["p"] = params
        return httpx.Response(200, json={})

    monkeypatch.setattr(srv.httpx, "get", fake_get)
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
        (404, "TEMPO_DATASOURCE_UID"),
        (500, "500"),
    ],
)
def test_http_errors_become_readable_strings(monkeypatch, status, must_contain):
    srv = _load()
    monkeypatch.setattr(
        srv.httpx, "get", lambda *a, **kw: httpx.Response(status, text="boom")
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
    monkeypatch.setattr(
        srv.httpx,
        "get",
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
    monkeypatch.setattr(srv.httpx, "get", lambda *a, **kw: resp)
    out = srv.get_trace("abc123")
    assert isinstance(out, str)
    assert "cap" in out


def test_a_normal_sized_trace_is_still_returned(monkeypatch):
    # The bound must not swallow the ordinary case; a grader that only checks
    # the refusal would pass with the cap set to zero.
    srv = _load()
    payload = {"batches": [{"spans": [{"name": "GET /health"}]}]}
    monkeypatch.setattr(srv.httpx, "get", lambda *a, **kw: httpx.Response(200, json=payload))
    assert srv.get_trace("abc123") == payload


def test_search_results_are_capped_too(monkeypatch):
    # The bound lives in _proxy rather than in get_trace, so every tool is
    # covered -- a wide TraceQL search can also return more than the ceiling.
    srv = _load()
    oversized = str(srv.MAX_RESPONSE_BYTES + 1)
    monkeypatch.setattr(
        srv.httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200, json={"traces": []}, headers={"content-length": oversized}
        ),
    )
    assert "cap" in srv.search_traces("{}")


def test_auth_failure_says_it_will_not_fix_itself(monkeypatch):
    # Without this the agent retries a 403 three times and reports a timeout.
    srv = _load()
    monkeypatch.setattr(srv.httpx, "get", lambda *a, **kw: httpx.Response(403))
    assert "not fix itself" in srv.search_traces("{}")


def test_timeout_is_not_an_exception(monkeypatch):
    srv = _load()

    def boom(*a, **kw):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(srv.httpx, "get", boom)
    out = srv.search_traces("{}")
    assert isinstance(out, str) and "did not respond" in out


def test_transport_error_does_not_leak_the_token(monkeypatch):
    srv = _load()

    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(srv.httpx, "get", boom)
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
