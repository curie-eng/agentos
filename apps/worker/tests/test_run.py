"""Substrate selection + the local-middle-mode fail-closed credential gate.

Local middle mode (Docker substrate) defaults to a real model; fake model is an
explicit opt-in. A Docker worker with neither a model credential nor
CURIE_FAKE_MODEL must fail loudly instead of silently degrading to a fake.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from curie_worker.config import WorkerConfig
from curie_worker.run import _sandbox_client, _substrate_config, _supervise, main
from curie_worker.sandbox import DockerSandboxClient, SubstrateConfig

_SUB = SubstrateConfig(namespace="default", warm_pool="pool")

# A non-secret placeholder credential. The docker fail-closed gate only checks
# that a credential env var is PRESENT, so the value is irrelevant; keep it an
# obvious placeholder behind a named constant so the secret scanner never
# mistakes it for a real token.
_FAKE_SDK_CRED = "oauth-PLACEHOLDER"


def test_substrate_config_claim_timeout_defaults_to_90s() -> None:
    assert _substrate_config({}).claim_timeout_seconds == 90.0


def test_substrate_config_claim_timeout_reads_env() -> None:
    cfg = _substrate_config({"CURIE_CLAIM_TIMEOUT_SECONDS": "45"})
    assert cfg.claim_timeout_seconds == 45.0


def test_substrate_config_route_ttls_default_unchanged() -> None:
    # Exposing these must not change behaviour for anyone who sets nothing.
    cfg = _substrate_config({})
    assert cfg.route_ttl_seconds == 3600
    assert cfg.suspended_route_ttl_seconds == 86400


def test_substrate_config_route_ttl_reads_env() -> None:
    cfg = _substrate_config({"CURIE_ROUTE_TTL_SECONDS": "300"})
    assert cfg.route_ttl_seconds == 300


def test_substrate_config_suspended_route_ttl_reads_env() -> None:
    cfg = _substrate_config({"CURIE_SUSPENDED_ROUTE_TTL_SECONDS": "7200"})
    assert cfg.suspended_route_ttl_seconds == 7200


def test_route_ttl_override_is_independent_of_claim_timeout() -> None:
    # The regression this whole change exists for (#1380): an operator could
    # reach the DEADLINE but not the ACCUMULATION term, so the only available
    # lever made a doomed turn fail slower instead of reducing how many
    # sandboxes were alive. Setting one must not disturb the other.
    cfg = _substrate_config(
        {"CURIE_ROUTE_TTL_SECONDS": "300", "CURIE_CLAIM_TIMEOUT_SECONDS": "45"}
    )
    assert cfg.route_ttl_seconds == 300
    assert cfg.claim_timeout_seconds == 45.0
    assert cfg.suspended_route_ttl_seconds == 86400


def test_claim_timeout_default_stays_under_lock_ttl() -> None:
    # The claim is the dominant term in the per-thread critical section; it must
    # stay below the lock TTL so the lock never lapses mid-claim.
    assert _SUB.claim_timeout_seconds < WorkerConfig().lock_ttl_ms / 1000


def test_valkey_socket_timeout_exceeds_the_block_interval() -> None:
    # redis-py enforces the client socket_timeout on the blocking XREADGROUP, so
    # it must sit above read_block_ms or every idle read raises a timeout instead
    # of returning empty (log flood). Guard the invariant that keeps idle reads
    # quiet across any read_block_ms tuning.
    cfg = WorkerConfig()
    assert cfg.valkey_socket_timeout_s > cfg.read_block_ms / 1000


def test_docker_without_credential_or_fake_fails_loudly() -> None:
    with pytest.raises(SystemExit) as exc:
        _sandbox_client(WorkerConfig(), {"CURIE_SANDBOX_SUBSTRATE": "docker"}, _SUB)
    msg = str(exc.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in msg  # tells the user how to fix it
    assert "CURIE_FAKE_MODEL" in msg


def test_docker_with_sdk_credential_builds_docker_client(monkeypatch) -> None:
    # Keep hermetic: after Stream B, _sandbox_client prewarms the image via
    # DockerSandboxClient.ensure_image; stub it so this test never shells docker.
    monkeypatch.setattr(
        DockerSandboxClient, "ensure_image", lambda self: None, raising=False
    )
    client = _sandbox_client(
        WorkerConfig(),
        {"CURIE_SANDBOX_SUBSTRATE": "docker", "CLAUDE_CODE_OAUTH_TOKEN": _FAKE_SDK_CRED},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_curie_credentials_reference_builds_docker_client(monkeypatch) -> None:
    # CURIE_CREDENTIALS alone is a valid credential: forwarded by name and
    # mapped onto an SDK var by the runner, so the gate must accept it.
    monkeypatch.setattr(
        DockerSandboxClient, "ensure_image", lambda self: None, raising=False
    )
    client = _sandbox_client(
        WorkerConfig(credentials="sk-ant-PLACEHOLDER"),
        {"CURIE_SANDBOX_SUBSTRATE": "docker"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_model_base_url_builds_docker_client_without_credential(monkeypatch) -> None:
    monkeypatch.setattr(
        DockerSandboxClient, "ensure_image", lambda self: None, raising=False
    )
    client = _sandbox_client(
        WorkerConfig(model_base_url="http://ollama:11434"),
        {"CURIE_SANDBOX_SUBSTRATE": "docker"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_explicit_fake_model_builds_docker_client(monkeypatch) -> None:
    monkeypatch.setattr(
        DockerSandboxClient, "ensure_image", lambda self: None, raising=False
    )
    client = _sandbox_client(
        WorkerConfig(fake_model=True), {"CURIE_SANDBOX_SUBSTRATE": "docker"}, _SUB
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_without_otlp_endpoint_warns(caplog) -> None:
    # Docker substrate exports runner traces via OTLP; without an endpoint the
    # traces silently go nowhere, so the boot must warn (not fail).
    with caplog.at_level(logging.WARNING, logger="curie_worker.run"):
        client = _sandbox_client(
            WorkerConfig(fake_model=True), {"CURIE_SANDBOX_SUBSTRATE": "docker"}, _SUB
        )
    assert isinstance(client, DockerSandboxClient)
    warnings = [
        r for r in caplog.records
        if r.name == "curie_worker.run" and "OTEL_EXPORTER_OTLP_ENDPOINT" in r.getMessage()
    ]
    assert warnings and all(r.levelno == logging.WARNING for r in warnings)


def test_docker_with_otlp_endpoint_does_not_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="curie_worker.run"):
        client = _sandbox_client(
            WorkerConfig(fake_model=True),
            {
                "CURIE_SANDBOX_SUBSTRATE": "docker",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
            },
            _SUB,
        )
    assert isinstance(client, DockerSandboxClient)
    assert not [
        r for r in caplog.records
        if r.name == "curie_worker.run" and "OTEL_EXPORTER_OTLP_ENDPOINT" in r.getMessage()
    ]


def test_sandbox_client_docker_prepulls_image(monkeypatch) -> None:
    # _sandbox_client must prewarm the runner image exactly once at startup,
    # inside the docker branch, so the first claim is not gated on a cold pull.
    calls: list[object] = []
    monkeypatch.setattr(
        DockerSandboxClient,
        "ensure_image",
        lambda self: calls.append(self),
        raising=False,
    )
    _sandbox_client(
        WorkerConfig(),
        {"CURIE_SANDBOX_SUBSTRATE": "docker", "CLAUDE_CODE_OAUTH_TOKEN": _FAKE_SDK_CRED},
        _SUB,
    )
    assert len(calls) == 1


def test_main_installs_dead_letter_alerting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_logger = logging.getLogger("curie_worker.consumer")
    original_handlers = list(source_logger.handlers)
    original_propagate = source_logger.propagate
    for handler in original_handlers:
        source_logger.removeHandler(handler)
    source_logger.propagate = False

    captured_coroutines: list[Any] = []

    def capture_run(coroutine: Any) -> None:
        captured_coroutines.append(coroutine)

    monkeypatch.setattr(asyncio, "run", capture_run)
    caplog.clear()

    try:
        with caplog.at_level(logging.ERROR):
            main({})
            source_logger.error(
                "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
                "1730000000000-0",
                2,
                "max-delivery-exceeded",
                "curie:runs:dead",
            )

        assert len(captured_coroutines) == 1
        alerts = [
            record
            for record in caplog.records
            if record.name == "curie_worker.alerts.dead_letter"
            and record.levelno == logging.CRITICAL
        ]
        assert len(alerts) == 1, f"expected one dead letter alert, got {alerts}"
    finally:
        for coroutine in captured_coroutines:
            coroutine.close()
        for handler in list(source_logger.handlers):
            source_logger.removeHandler(handler)
        for handler in original_handlers:
            source_logger.addHandler(handler)
        source_logger.propagate = original_propagate


# -- _supervise: per-task restart + sibling isolation (#673) -----------------


def test_supervise_restarts_a_crashing_task_until_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A consumer that crashes is restarted rather than allowed to escape. The
    task settles once it returns cleanly (its own stop was requested)."""

    async def go() -> None:
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def factory() -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("boom")
            # Third run behaves like a real consumer: return once stopped.
            shutdown.set()

        with caplog.at_level(logging.ERROR, logger="curie_worker.run"):
            await asyncio.wait_for(
                _supervise("evals", factory, shutdown, restart_backoff_s=0),
                timeout=2,
            )

        assert calls["n"] == 3  # crashed twice, restarted twice, then returned
        restarts = [
            r
            for r in caplog.records
            if r.name == "curie_worker.run" and "crashed; restarting" in r.getMessage()
        ]
        assert len(restarts) == 2

    asyncio.run(go())


def test_supervise_does_not_restart_after_shutdown() -> None:
    """A crash arriving as shutdown is requested must not trigger a restart, and
    must not propagate out of the supervisor."""

    async def go() -> None:
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def factory() -> None:
            calls["n"] += 1
            shutdown.set()
            raise ConnectionError("boom")

        await asyncio.wait_for(
            _supervise("runs", factory, shutdown, restart_backoff_s=0),
            timeout=2,
        )
        assert calls["n"] == 1  # no restart after shutdown

    asyncio.run(go())


def test_supervise_returns_when_task_completes_cleanly() -> None:
    async def go() -> None:
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def factory() -> None:
            calls["n"] += 1

        await asyncio.wait_for(
            _supervise("heartbeat", factory, shutdown, restart_backoff_s=0),
            timeout=2,
        )
        assert calls["n"] == 1  # returned on first clean completion, no restart

    asyncio.run(go())


def test_crashing_supervised_task_does_not_cancel_its_siblings() -> None:
    """The #673 core: one consumer crashing (and restarting) must not tear down a
    sibling consumer sharing the same event loop under the top-level gather."""

    async def go() -> None:
        shutdown = asyncio.Event()
        sibling_ran = {"ok": False}
        crashes = {"n": 0}

        async def crasher() -> None:
            crashes["n"] += 1
            if crashes["n"] >= 3:
                shutdown.set()  # last crash also asks everything to stop
            raise ConnectionError("boom")

        async def sibling() -> None:
            # Only completes if it was NOT cancelled by the crasher's failure.
            await shutdown.wait()
            sibling_ran["ok"] = True

        await asyncio.wait_for(
            asyncio.gather(
                _supervise("crasher", crasher, shutdown, restart_backoff_s=0),
                _supervise("sibling", sibling, shutdown, restart_backoff_s=0),
                return_exceptions=True,
            ),
            timeout=2,
        )

        assert crashes["n"] == 3
        assert sibling_ran["ok"] is True

    asyncio.run(go())
