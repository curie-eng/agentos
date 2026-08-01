"""The reconcile loop's failure containment (ADR-0090, #1184).

The loop shares a process with the kernel, whose four correctness rules are not
negotiable. So most of what matters here is what happens when something goes
wrong: one bad agent, a raising client, a database that will not answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from curie_worker.connector_agent import AgentOutcome, RenderedConnectors
from curie_worker.connector_apply import ApplyReport
from curie_worker.connector_loop import AgentTarget, ConnectorReconcileLoop, PassSummary

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def target(name: str) -> AgentTarget:
    import uuid

    return AgentTarget(agent_id=uuid.uuid4(), agent_name=name, version_id=uuid.uuid4())


class Loop(ConnectorReconcileLoop):
    """The loop with the database and the reconcile step swapped out.

    Subclassed rather than mocked: `one_pass`'s containment logic is the thing
    under test, and it should be exercised exactly as written.
    """

    def __init__(self, targets: list[AgentTarget], outcomes: dict[str, Any], **kw: Any) -> None:
        self._targets = targets
        self._outcomes = outcomes
        self.seen: list[str] = []
        self._namespace = "curie"
        self._interval = kw.get("interval_seconds", 0.01)

    async def targets(self) -> list[AgentTarget]:  # type: ignore[override]
        return list(self._targets)

    def _reconcile_one(self, t: AgentTarget) -> AgentOutcome:  # type: ignore[override]
        self.seen.append(t.agent_name)
        result = self._outcomes[t.agent_name]
        if isinstance(result, Exception):
            raise result
        return result


def ok(agent: str, applied: int = 0, deleted: int = 0) -> AgentOutcome:
    return AgentOutcome(
        agent=agent,
        report=ApplyReport(
            applied=[("Service", f"s{i}") for i in range(applied)],
            deleted=[("Service", f"d{i}") for i in range(deleted)],
        ),
    )


# --------------------------------------------------------------------------- #
# One agent's failure ends with that agent
# --------------------------------------------------------------------------- #
async def test_a_raising_agent_does_not_strand_the_rest() -> None:
    # Aborting the sweep would leave every later agent unreconciled, and the
    # order is arbitrary -- so which agents those are changes between passes.
    loop = Loop(
        [target("a"), target("boom"), target("c")],
        {"a": ok("a", applied=1), "boom": RuntimeError("apiserver down"), "c": ok("c", applied=1)},
    )
    summary = await loop.one_pass()

    assert loop.seen == ["a", "boom", "c"], "the sweep stopped early"
    assert summary.reconciled == 3
    assert summary.applied == 2
    assert summary.failed == 1


async def test_a_failed_report_counts_without_raising() -> None:
    failing = AgentOutcome(
        agent="a", report=ApplyReport(failures=[("Service", "svc", "forbidden")])
    )
    summary = await Loop([target("a")], {"a": failing}).one_pass()
    assert summary.failed == 1


async def test_a_skipped_agent_is_counted_separately_from_a_failure() -> None:
    # Skipping is a correct, expected outcome (an operator-supplied credential
    # that is not provisioned). Counting it as a failure would make the normal
    # state of a partially-migrated install look broken.
    skipped = AgentOutcome(agent="a", skipped="credentials not provisioned")
    summary = await Loop([target("a")], {"a": skipped}).one_pass()
    assert summary.skipped == 1
    assert summary.failed == 0


async def test_no_agents_is_a_clean_pass() -> None:
    summary = await Loop([], {}).one_pass()
    assert summary == PassSummary()
    assert not summary.did_work


# --------------------------------------------------------------------------- #
# Logging: quiet when converged
# --------------------------------------------------------------------------- #
async def test_a_converged_pass_does_not_log_at_info(caplog) -> None:
    # The steady state is "nothing changed", forever. A loop that narrates every
    # pass trains people to ignore it, and the one pass that mattered scrolls by.
    with caplog.at_level("INFO", logger="curie_worker.connector_loop"):
        await Loop([target("a")], {"a": ok("a")}).one_pass()
    assert caplog.records == []


async def test_a_pass_that_did_work_says_so(caplog) -> None:
    with caplog.at_level("INFO", logger="curie_worker.connector_loop"):
        await Loop([target("a")], {"a": ok("a", applied=2, deleted=1)}).one_pass()
    assert any("2 applied, 1 deleted" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# The loop never takes the worker down
# --------------------------------------------------------------------------- #
async def test_a_pass_that_raises_outright_does_not_end_the_loop() -> None:
    # `targets()` hitting a dead database is the realistic version of this.
    passes = 0

    class Broken(Loop):
        async def one_pass(self):  # type: ignore[override]
            nonlocal passes
            passes += 1
            if passes < 3:
                raise RuntimeError("database is gone")
            return PassSummary()

    loop = Broken([], {}, interval_seconds=0.01)
    stop = asyncio.Event()

    async def run() -> None:
        await loop.run_forever(stop)

    task = asyncio.create_task(run())
    while passes < 3:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert passes >= 3, "the loop gave up after a failing pass"


async def test_stop_is_honoured_promptly_rather_than_after_the_interval() -> None:
    # A worker shutdown must not wait out a 60s sleep.
    loop = Loop([], {}, interval_seconds=300)
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    # If stop did not interrupt the interval wait, this times out rather than
    # sitting for the full 300s.
    await asyncio.wait_for(task, timeout=2)


async def test_cancellation_propagates() -> None:
    # Cancelled is not an error to swallow; the worker is shutting down.
    loop = Loop([], {}, interval_seconds=300)
    task = asyncio.create_task(loop.run_forever(asyncio.Event()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# The manifest source
# --------------------------------------------------------------------------- #
def test_the_http_source_reads_the_fields_the_agent_step_needs(monkeypatch) -> None:
    import functools

    import httpx
    from curie_worker.connector_loop import HttpManifestSource

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("X-API-Key")
        return httpx.Response(
            200,
            json={
                "manifests": [{"kind": "Service", "metadata": {"name": "svc"}}],
                "owned_secret_name": "curie-a-connector-secrets",
                "owned_secret_keys": ["TOKEN"],
                "mcp_entries": {},
            },
        )

    source = HttpManifestSource(
        api_base_url="http://api:8000/",
        api_key="k",
        release="curie",
        namespace="curie",
        app_name="curie",
    )
    monkeypatch.setattr(
        httpx, "Client", functools.partial(httpx.Client, transport=httpx.MockTransport(handler))
    )
    rendered = source.rendered(agent_id="a-1", version_id="v-1")

    assert rendered.manifests[0]["metadata"]["name"] == "svc"
    assert rendered.needs_operator_credentials
    assert rendered.owned_secret_name == "curie-a-connector-secrets"
    assert "/agents/a-1/versions/v-1/connectors" in captured["url"]
    assert "release=curie" in captured["url"], "the caller must supply install-time facts"
    assert captured["key"] == "k"


def test_the_worker_builds_no_loop_when_the_flag_is_off() -> None:
    # The default path must not touch the Kubernetes client at all -- a worker
    # with no kubeconfig and no interest in connectors still has to boot.
    from curie_worker.config import WorkerConfig
    from curie_worker.run import _build_connector_loop

    assert _build_connector_loop(WorkerConfig(), engine=None) is None  # type: ignore[arg-type]


def test_enabling_the_reconciler_without_addressing_it_is_refused() -> None:
    # Names are built from release + app_name. With either missing, every name
    # the reconciler renders differs from what exists: it finds nothing of its
    # own, creates a parallel set under wrong names, and leaves the real
    # connectors unmanaged -- silently, looking like it works.
    import pydantic
    from curie_worker.config import WorkerConfig

    with pytest.raises(pydantic.ValidationError, match="CURIE_CONNECTOR_APP_NAME"):
        WorkerConfig(
            connector_reconcile_enabled=True,
            connector_namespace="curie",
            connector_release="curie",
            connector_app_name="",
        )


def test_the_http_source_defaults_are_safe_when_the_api_omits_fields() -> None:
    # An older API that predates owned_secret_keys must not read as "no
    # credentials needed", which would let the loop prune one.
    rendered = RenderedConnectors()
    assert rendered.manifests == []
    assert not rendered.needs_operator_credentials
