"""Proxy endpoint tests with the external Langfuse HTTP calls mocked.

Only the external Langfuse API is mocked (permitted); the reconstruction and the
FastAPI wiring are exercised for real.
"""

from typing import Any

from curie_api.deps import get_langfuse
from curie_api.langfuse import matching_traces
from curie_api.main import create_app
from fastapi.testclient import TestClient


class FakeLangfuse:
    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self._observations = observations
        self.list_calls: list[tuple[int, str | None]] = []

    async def get_observations(self, trace_id: str) -> list[dict[str, Any]]:
        return self._observations

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return {"id": trace_id, "name": "demo"}

    async def list_traces(
        self, limit: int, name_contains: str | None = None
    ) -> list[dict[str, Any]]:
        self.list_calls.append((limit, name_contains))
        return [{"id": "t1", "name": "curie-run:agent-x-thread-1"}]


def _app_with(observations: list[dict[str, Any]]) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_langfuse] = lambda: FakeLangfuse(observations)
    return TestClient(app)


def test_get_trace_returns_reconstructed_tree(
    auth_headers: dict[str, str],
) -> None:
    observations = [
        {"id": "r", "type": "SPAN", "name": "agent.run", "startTime": "1"},
        {
            "id": "g",
            "type": "GENERATION",
            "name": "gen",
            "startTime": "2",
            "parentObservationId": "r",
        },
    ]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace"]["id"] == "abc"
    assert body["tree"][0]["name"] == "agent.run"
    assert body["tree"][0]["children"][0]["type"] == "GENERATION"


def test_get_trace_surfaces_sandbox_id_from_observation(
    auth_headers: dict[str, str],
) -> None:
    # Platform spans now precede agent.run. A first-observation probe returns
    # the deliberately wrong value; the runner root is the authority.
    observations = [
        {
            "id": "platform",
            "type": "SPAN",
            "name": "POST /channels/turns",
            "startTime": "1",
            "metadata": {"curie.sandbox_id": "sbx-wrong-platform"},
            "resourceAttributes": {"service.name": "curie-api"},
        },
        {
            "id": "r",
            "type": "SPAN",
            "name": "agent.run",
            "startTime": "2",
            "parentObservationId": "platform",
            "metadata": {"curie.sandbox_id": "sbx-proxy"},
            "resourceAttributes": {"service.name": "curie-runner"},
        },
    ]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["sandbox_id"] == "sbx-proxy"


def test_get_trace_sandbox_id_null_when_absent(
    auth_headers: dict[str, str],
) -> None:
    observations = [{"id": "r", "type": "SPAN", "name": "agent.run", "startTime": "1"}]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["sandbox_id"] is None


def test_get_trace_surfaces_approval_decision_from_observation(
    auth_headers: dict[str, str],
) -> None:
    # Same precedence contract as sandbox_id: the runner agent.run observation
    # wins after an earlier platform observation.
    observations = [
        {
            "id": "platform",
            "type": "SPAN",
            "name": "send curie:runs",
            "startTime": "1",
            "metadata": {"gen_ai.approval.decision": "rejected"},
            "resourceAttributes": {"service.name": "curie-api"},
        },
        {
            "id": "r",
            "type": "SPAN",
            "name": "agent.run",
            "startTime": "2",
            "parentObservationId": "platform",
            "metadata": {"gen_ai.approval.decision": "approved"},
            "resourceAttributes": {"service.name": "curie-runner"},
        },
    ]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["approval_decision"] == "approved"


def test_get_trace_approval_decision_null_when_absent(
    auth_headers: dict[str, str],
) -> None:
    # The ordinary case: no approval was resumed this turn.
    observations = [{"id": "r", "type": "SPAN", "name": "agent.run", "startTime": "1"}]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["approval_decision"] is None


def test_get_trace_404_when_no_observations(
    auth_headers: dict[str, str],
) -> None:
    with _app_with([]) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 404


def test_get_trace_rejects_malformed_trace_id(
    auth_headers: dict[str, str],
) -> None:
    # A trace id carrying path metacharacters is refused before it can reach
    # Langfuse's URL path (no `.`/`..`/`/` traversal into other upstream paths).
    with _app_with([]) as client:
        resp = client.get("/langfuse/traces/abc.def", headers=auth_headers)
    assert resp.status_code == 400


def test_proxy_requires_api_key() -> None:
    with _app_with([]) as client:
        resp = client.get("/langfuse/traces/abc")
    assert resp.status_code == 401


def test_list_traces_without_agent_id_does_not_filter(
    auth_headers: dict[str, str],
) -> None:
    fake = FakeLangfuse([])
    app = create_app()
    app.dependency_overrides[get_langfuse] = lambda: fake
    with TestClient(app) as client:
        resp = client.get("/langfuse/traces", headers=auth_headers)
    assert resp.status_code == 200
    # No agent_id -> the substring filter is None (all recent traces).
    assert fake.list_calls == [(20, None)]


def test_list_traces_with_agent_id_filters_by_the_agent_token(
    auth_headers: dict[str, str],
) -> None:
    fake = FakeLangfuse([])
    app = create_app()
    app.dependency_overrides[get_langfuse] = lambda: fake
    agent_id = "11111111-1111-1111-1111-111111111111"
    with TestClient(app) as client:
        resp = client.get(
            "/langfuse/traces",
            params={"agent_id": agent_id, "limit": 5},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    # agent_id -> the client is asked to filter by the `agent-<id>` token.
    assert fake.list_calls == [(5, f"agent-{agent_id}")]


def test_matching_traces_keeps_only_the_token_and_caps_at_limit() -> None:
    traces = [
        {"id": "1", "name": "curie-run:agent-A-thread-1"},
        {"id": "2", "name": "curie-run:agent-B-thread-1"},
        {"id": "3", "name": "curie-run:agent-A-thread-2"},
        {"id": "4", "name": None},  # defensive: non-string name is skipped
        {"id": "5", "name": "curie-run:agent-A-thread-3"},
    ]
    matched = matching_traces(traces, "agent-A", limit=2)
    assert [t["id"] for t in matched] == ["1", "3"]  # newest-first order preserved, capped
    assert matching_traces(traces, "agent-Z", limit=10) == []
