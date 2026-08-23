"""Substrate selection + the local-middle-mode fail-closed credential gate.

Local middle mode (Docker substrate) defaults to a real model; fake model is an
explicit opt-in. A Docker worker with neither a model credential nor
CURIE_FAKE_MODEL must fail loudly instead of silently degrading to a fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import curie_worker.run as run_module
import pytest
from curie_worker import __version__
from curie_worker.config import WorkerConfig
from curie_worker.run import (
    _MAX_TUNABLE_SECONDS,
    _sandbox_client,
    _substrate_config,
    _supervise,
    main,
)
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


# --- #1388: the three operator-tunable seconds knobs are bounded at both ends ---
#
# Every assertion below goes through _substrate_config, the loader build() calls
# at run.py:180 -- never through whatever private helper implements the bound.
# A test bound to that helper would still pass if the helper were disconnected
# from the loader, which is the regression these exist to catch.

# The documented ceiling, 365 days. Duplicated as a JSON Schema literal in
# charts/curie/values.schema.json; the accept/reject pair below is one half of
# the four-assertion net that keeps the two literals from drifting apart (the
# chart half is assertions (e) and (h) of
# charts/curie/ci/worker-ttl-bounds-assertions.sh).
_MAX_SECONDS = "31536000"
_OVER_MAX_SECONDS = "31536001"


def test_substrate_config_defaults_unchanged_by_the_bounds_guard() -> None:
    # Full frozen-dataclass equality rather than field-by-field: it pins EVERY
    # field at once, so bounding the three knobs cannot quietly move a default
    # for the installs that set nothing.
    assert _substrate_config({}) == SubstrateConfig(
        namespace="default", warm_pool="curie-runner-pool"
    )


@pytest.mark.parametrize(
    ("var", "raw"),
    [
        ("CURIE_ROUTE_TTL_SECONDS", "0"),
        ("CURIE_ROUTE_TTL_SECONDS", "-1"),
        ("CURIE_ROUTE_TTL_SECONDS", "-5"),
        ("CURIE_SUSPENDED_ROUTE_TTL_SECONDS", "0"),
        ("CURIE_SUSPENDED_ROUTE_TTL_SECONDS", "-1"),
        ("CURIE_SUSPENDED_ROUTE_TTL_SECONDS", "-5"),
        ("CURIE_CLAIM_TIMEOUT_SECONDS", "0"),
        ("CURIE_CLAIM_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_substrate_config_refuses_a_non_positive_knob(var: str, raw: str) -> None:
    # A non-positive value boots healthy today and fails on the FIRST message,
    # unclassified: route_ttl 0 makes AffinityStore.put_if_absent issue
    # SET ... NX EX 0, which real Valkey refuses. The refusal must name the env
    # var so an operator can act on it without reading the traceback.
    with pytest.raises(ValueError) as exc:
        _substrate_config({var: raw})
    assert var in str(exc.value)
    # repr(raw), not raw: the bare "0" params are a substring of the 31536000 in
    # the message's range text, so `raw in ...` would pass against a message that
    # never echoed the operator's value at all. The message formats with {raw!r},
    # so the quoted form is exact for every param and appears nowhere else.
    assert repr(raw) in str(exc.value)


@pytest.mark.parametrize(
    "var",
    [
        "CURIE_ROUTE_TTL_SECONDS",
        "CURIE_SUSPENDED_ROUTE_TTL_SECONDS",
        "CURIE_CLAIM_TIMEOUT_SECONDS",
    ],
)
def test_substrate_config_refuses_above_the_documented_maximum(var: str) -> None:
    with pytest.raises(ValueError) as exc:
        _substrate_config({var: _OVER_MAX_SECONDS})
    assert var in str(exc.value)
    assert _OVER_MAX_SECONDS in str(exc.value)


@pytest.mark.parametrize(
    ("var", "field", "expected"),
    [
        ("CURIE_ROUTE_TTL_SECONDS", "route_ttl_seconds", 31536000),
        ("CURIE_SUSPENDED_ROUTE_TTL_SECONDS", "suspended_route_ttl_seconds", 31536000),
        ("CURIE_CLAIM_TIMEOUT_SECONDS", "claim_timeout_seconds", 31536000.0),
    ],
)
def test_substrate_config_accepts_exactly_the_documented_maximum(
    var: str, field: str, expected: float
) -> None:
    # The accept side of the boundary. Paired with the refusal above, it proves
    # the bound is inclusive at 31536000 and not off by one, and it is what stops
    # the Python constant and the chart schema's `maximum` from drifting apart.
    assert getattr(_substrate_config({var: _MAX_SECONDS}), field) == expected


def test_chart_schema_bounds_match_the_python_bound() -> None:
    # The direct pin on the cross-language seam AGENTS.md names: a deploy-time
    # validator and the runtime loader that re-parses the same value. The
    # behavioural boundary pairs above and assertions (e)/(h) of
    # charts/curie/ci/worker-ttl-bounds-assertions.sh read the seam from two
    # ends; this reads the chart's own JSON and compares the literals, so
    # moving either side alone fails here rather than shipping a helm that
    # accepts a value the worker refuses at boot (or the reverse).
    #
    # Resolved from this file's location, not the working directory, so it
    # holds whether pytest runs from the repo root or from apps/worker.
    repo_root = Path(__file__).resolve().parents[3]
    schema_path = repo_root / "charts" / "curie" / "values.schema.json"
    worker = json.loads(schema_path.read_text())["properties"]["worker"]["properties"]
    drift = (
        f"{schema_path} and _MAX_TUNABLE_SECONDS in curie_worker/run.py have "
        f"drifted apart. They are the same bound expressed in two languages and "
        f"must move together: helm refuses the value at install time, the worker "
        f"refuses it at boot, and changing one alone makes a chart the worker "
        f"will not start under."
    )
    for knob in ("claimTimeoutSeconds", "routeTtlSeconds", "suspendedRouteTtlSeconds"):
        maximum = worker[knob]["maximum"]
        exclusive_minimum = worker[knob]["exclusiveMinimum"]
        # isinstance(True, int) is True in Python, so a draft-4 style rewrite of
        # the schema ("minimum": 0, "exclusiveMinimum": false -- which under
        # draft-4 semantics legalizes the value 0, exactly what this branch
        # exists to forbid) would satisfy `== 0` / `== _MAX_TUNABLE_SECONDS`
        # without this bool exclusion, since False == 0 and (depending on the
        # rewrite) True could stand in for a truthy bound. Require a real
        # number, not a bool wearing one.
        assert (
            isinstance(exclusive_minimum, (int, float))
            and not isinstance(exclusive_minimum, bool)
            and exclusive_minimum == 0
        ), f"{knob}: {drift}"
        assert (
            isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and maximum == _MAX_TUNABLE_SECONDS
        ), f"{knob}: {drift}"


@pytest.mark.parametrize("raw", ["abc", "3600.5", ""])
def test_substrate_config_refuses_an_unparseable_ttl_naming_the_env_var(raw: str) -> None:
    # int("abc") already raises ValueError today, so `pytest.raises(ValueError)`
    # alone would pass against the unguarded loader. The env-var-name assertion
    # is the load-bearing half: the bare message
    # "invalid literal for int() with base 10: 'abc'" names nothing an operator
    # can act on. The "" case is the only gate on the helm explicit-null path --
    # `--set worker.routeTtlSeconds=null` renders CURIE_ROUTE_TTL_SECONDS present
    # with an empty value, which the chart schema structurally cannot see (pinned
    # from the other end by assertion (i) of
    # charts/curie/ci/worker-ttl-bounds-assertions.sh).
    with pytest.raises(ValueError) as exc:
        _substrate_config({"CURIE_ROUTE_TTL_SECONDS": raw})
    assert "CURIE_ROUTE_TTL_SECONDS" in str(exc.value)


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "1e400"])
def test_substrate_config_refuses_a_non_finite_claim_timeout(raw: str) -> None:
    # The float-only failure mode a positivity check alone misses. float("inf")
    # (and float("1e400"), which overflows to inf) makes
    # `deadline = time.monotonic() + claim_timeout_seconds` at
    # sandbox/substrate.py:217 never elapse, so the claim wait spins forever
    # inside the per-thread lock. float("nan") is the mirror image: every `<`
    # comparison against it is False, so the wait loop never runs and every
    # claim fails instantly. Neither is caught by `value > 0`.
    with pytest.raises(ValueError) as exc:
        _substrate_config({"CURIE_CLAIM_TIMEOUT_SECONDS": raw})
    assert "CURIE_CLAIM_TIMEOUT_SECONDS" in str(exc.value)
    # The range check rejects all four on its own (inf fails the upper bound;
    # -inf and nan fail `0 < value`), so `pytest.raises(ValueError)` plus the
    # env-var name would pass with the non-finite branch deleted. Pinning the
    # word "finite" is the only observable difference between the two
    # implementations, so it is what makes this test discriminate.
    assert "finite" in str(exc.value)
    assert repr(raw) in str(exc.value)


@pytest.mark.parametrize(
    "var",
    [
        "CURIE_ROUTE_TTL_SECONDS",
        "CURIE_SUSPENDED_ROUTE_TTL_SECONDS",
        "CURIE_CLAIM_TIMEOUT_SECONDS",
    ],
)
def test_substrate_config_refuses_a_huge_integer_literal(var: str) -> None:
    # An operator typo of the leaning-on-the-zero-key kind. int() accepts an
    # arbitrarily large literal, and math.isfinite() converts its argument to a
    # C double, so on the int knobs an unnarrowed isfinite() raises
    # OverflowError -- an ArithmeticError, not a ValueError -- which escapes the
    # loader entirely and reaches the operator as "int too large to convert to
    # float", naming no env var and no value. The refusal must stay a ValueError
    # that names the knob, the same as every other out-of-range input.
    raw = "9" * 400
    with pytest.raises(ValueError) as exc:
        _substrate_config({var: raw})
    assert var in str(exc.value)
    assert repr(raw) in str(exc.value)


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


# -- #1817: worker telemetry bootstrap, relay, and bounded shutdown -----------


class _RecordingTelemetryRuntime:
    def __init__(self, order: list[str]) -> None:
        self.tracer = object()
        self.order = order
        self.shutdown_timeouts: list[int] = []

    def force_flush(self, *, timeout_millis: int) -> bool:
        self.order.append("telemetry-force-flush")
        return timeout_millis <= 2_000

    def shutdown(self, *, timeout_millis: int) -> bool:
        self.shutdown_timeouts.append(timeout_millis)
        self.order.append("telemetry-shutdown")
        return timeout_millis <= 2_000


def test_main_configures_worker_telemetry_and_shuts_down_after_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank standard endpoint is a normal no-op runtime, still lifecycle-owned.

    The shared package decides whether exporters exist; the worker must always
    use the same configure/shutdown seam so endpoint absence cannot grow a
    second code path that skips ordinary diagnostics or cleanup.
    """

    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)

    order: list[str] = []
    runtime = _RecordingTelemetryRuntime(order)
    configured: list[dict[str, object]] = []
    basic_config: list[dict[str, object]] = []

    def fake_configure(**kwargs: object) -> _RecordingTelemetryRuntime:
        configured.append(kwargs)
        order.append("telemetry-configured")
        return runtime

    async def fake_run(*args: object, **kwargs: object) -> None:
        # The runtime tracer is the only tracer main has available to hand into
        # build/_run.  Accept either a named tracer or the runtime itself so this
        # assertion stays about dependency propagation, not a private signature.
        flattened = (*args, *kwargs.values())
        assert runtime in flattened or runtime.tracer in flattened
        order.append("ordinary-runtime-finished")

    def fake_basic_config(**kwargs: object) -> None:
        basic_config.append(kwargs)

    monkeypatch.setattr(run_module, "configure", fake_configure, raising=False)
    monkeypatch.setattr(run_module, "_run", fake_run)
    monkeypatch.setattr(run_module, "install_dead_letter_alerting", lambda: None)
    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)

    run_module.main({})

    assert configured == [
        {"service_name": "curie-worker", "service_version": __version__}
    ]
    assert basic_config == [{"level": logging.INFO}]
    assert runtime.shutdown_timeouts == [2_000]
    assert order.index("ordinary-runtime-finished") < order.index("telemetry-shutdown")


def _docker_envs(client: DockerSandboxClient) -> list[str]:
    calls: list[list[str]] = []

    def record(args: list[str], *, check: bool = True) -> str:
        del check
        calls.append(args)
        return ""

    client._docker = record  # type: ignore[method-assign]
    client.create_claim(
        "runner-otel-example",
        pool="pool",
        env={"CURIE_FAKE_MODEL": "1"},
    )
    assert len(calls) == 1
    argv = calls[0]
    return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]


def test_docker_worker_uses_private_runner_relay_as_child_standard_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DockerSandboxClient,
        "ensure_image",
        lambda self: None,
        raising=False,
    )
    host_endpoint = "http://127.0.0.1:24318"
    runner_endpoint = "http://otel-collector:4318"
    client = _sandbox_client(
        WorkerConfig(fake_model=True),
        {
            "CURIE_SANDBOX_SUBSTRATE": "docker",
            "OTEL_EXPORTER_OTLP_ENDPOINT": host_endpoint,
            "CURIE_RUNNER_OTEL_EXPORTER_OTLP_ENDPOINT": runner_endpoint,
        },
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)

    child_env = _docker_envs(client)
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={runner_endpoint}" in child_env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf" in child_env
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={host_endpoint}" not in child_env
    assert "CURIE_RUNNER_OTEL_EXPORTER_OTLP_ENDPOINT" not in {
        entry.partition("=")[0] for entry in child_env
    }


def test_docker_runner_relay_defaults_to_standard_endpoint_outside_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DockerSandboxClient,
        "ensure_image",
        lambda self: None,
        raising=False,
    )
    endpoint = "http://collector.example.com:4318"
    client = _sandbox_client(
        WorkerConfig(fake_model=True),
        {
            "CURIE_SANDBOX_SUBSTRATE": "docker",
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        },
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)
    child_env = _docker_envs(client)
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={endpoint}" in child_env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf" in child_env


def test_docker_runner_with_both_endpoints_blank_injects_no_exporter_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DockerSandboxClient,
        "ensure_image",
        lambda self: None,
        raising=False,
    )
    client = _sandbox_client(
        WorkerConfig(fake_model=True),
        {
            "CURIE_SANDBOX_SUBSTRATE": "docker",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "CURIE_RUNNER_OTEL_EXPORTER_OTLP_ENDPOINT": "",
        },
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)
    child_names = {entry.partition("=")[0] for entry in _docker_envs(client)}
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in child_names
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in child_names
