"""Process entrypoint: wire the kernel and consumer, then run.

Reads the environment, builds the async Valkey client (stream, locks, markers),
a sync Valkey client for the substrate's affinity store, the sandbox substrate,
the runner HTTP client, and the Slack sink, then runs the consumer until a
signal asks it to stop. Run with ``python -m curie_worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import redis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .approval_cards import ApprovalCardStore
from .approvals import ApprovalClient
from .binding import BindingResolver
from .bundle_store import BundleStore
from .config import WorkerConfig
from .consumer import Consumer
from .dead_letter_alert import install_dead_letter_alerting
from .eval import EvalReporter, EvalStreamConsumer, LangfuseEvalRecorder
from .heartbeat import run_heartbeat
from .kernel import Kernel
from .killswitch import KillSwitch
from .markers import Markers
from .runner_client import RunnerClient
from .sandbox import (
    AffinityStore,
    DockerSandboxClient,
    KubernetesSandboxClient,
    RunnerHardening,
    SandboxClient,
    SandboxSubstrate,
    SubstrateConfig,
)
from .slack_sink import AsyncSlackSink
from .threadlock import ThreadLock

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """The wired worker: the two Valkey consumers (runs + evals) plus the
    resources whose lifetimes they share, so ``_run`` can drive and dispose them."""

    consumer: Consumer
    killswitch: KillSwitch
    eval_consumer: EvalStreamConsumer
    runner: RunnerClient
    async_redis: AsyncRedis
    eval_redis: AsyncRedis
    eval_http: httpx.AsyncClient
    engine: AsyncEngine


def _substrate_config(env: Mapping[str, str]) -> SubstrateConfig:
    # claim_timeout is overridable so a slow cluster can raise it; when unset the
    # authoritative default lives in SubstrateConfig. Keep any override below the
    # per-thread lock TTL (WorkerConfig.lock_ttl_ms) -- see that comment.
    overrides: dict[str, Any] = {}
    claim_timeout = env.get("CURIE_CLAIM_TIMEOUT_SECONDS")
    if claim_timeout is not None:
        overrides["claim_timeout_seconds"] = float(claim_timeout)
    return SubstrateConfig(
        namespace=env.get("CURIE_NAMESPACE", "default"),
        warm_pool=env.get("CURIE_WARM_POOL", "curie-runner-pool"),
        runner_port=int(env.get("CURIE_RUNNER_PORT", "8080")),
        **overrides,
    )


# The SDK credential env the runner authenticates a real model with; presence of
# either satisfies the local-middle-mode credential requirement.
_MODEL_CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _sandbox_client(
    config: WorkerConfig, env: Mapping[str, str], sub_config: SubstrateConfig
) -> SandboxClient:
    """The cluster/Docker seam, chosen by ``CURIE_SANDBOX_SUBSTRATE``.

    ``kubernetes`` (default) claims agent-sandbox CRs; ``docker`` boots runner
    containers locally (middle mode on a laptop, no cluster). The eval consumer
    shares the substrate this client backs, so the choice applies to both lanes.

    Local middle mode defaults to a REAL model. Fake model is an explicit
    offline/test opt-in, so a Docker worker with neither a model credential,
    ``CURIE_MODEL_BASE_URL``, nor ``CURIE_FAKE_MODEL`` fails loudly here
    rather than booting a real runner that would fail cryptically or silently
    degrading to a fake. A credential can be an SDK var
    (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``) or the ACI
    ``CURIE_CREDENTIALS`` reference, which the runner maps onto an SDK var.
    """
    substrate = env.get("CURIE_SANDBOX_SUBSTRATE", "kubernetes").lower()
    if substrate == "docker":
        has_credential = bool(config.credentials) or any(
            v in env for v in _MODEL_CREDENTIAL_ENV
        )
        has_local_model = bool(config.model_base_url)
        if not config.fake_model and not has_credential and not has_local_model:
            raise SystemExit(
                "Local middle mode (CURIE_SANDBOX_SUBSTRATE=docker) defaults to a "
                "real model, but no model credential is set. Export "
                "CURIE_CREDENTIALS, CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_API_KEY "
                "before starting the worker, set CURIE_MODEL_BASE_URL to a local "
                "endpoint for local-model mode, or set CURIE_FAKE_MODEL=1 for an "
                "offline/test run."
            )
        if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            logger.warning(
                "Docker substrate selected but OTEL_EXPORTER_OTLP_ENDPOINT is "
                "unset; runner traces will not be exported"
            )
        client = DockerSandboxClient(
            image=env.get("CURIE_RUNNER_IMAGE", "curie-runner"),
            bundle_store=BundleStore(config),
            network=env.get("CURIE_DOCKER_NETWORK") or None,
            otel_endpoint=env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            default_plugin_dir=config.bundle_plugin_dir,
            # Container isolation for every spawned runner (#631): read-only
            # rootfs, dropped caps, no-new-privileges, bounded resources. Mirrors
            # the K8s runner securityContext; overridable via CURIE_RUNNER_*.
            hardening=RunnerHardening.from_env(env),
            environ=env,
            bundle_max_uncompressed_bytes=config.bundle_max_uncompressed_bytes,
            bundle_max_compression_ratio=config.bundle_max_compression_ratio,
            bundle_max_members=config.bundle_max_members,
        )
        # Prewarm the runner image once at startup so the first claim window is
        # not gated on a cold pull. Best-effort inside ensure_image.
        client.ensure_image()
        return client
    return KubernetesSandboxClient(sub_config.namespace)


def build(config: WorkerConfig, env: Mapping[str, str]) -> Runtime:
    async_redis: AsyncRedis = AsyncRedis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
        socket_timeout=config.valkey_socket_timeout_s,
    )
    sync_redis = redis.Redis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
        socket_timeout=config.valkey_socket_timeout_s,
    )
    sub_config = _substrate_config(env)
    substrate = SandboxSubstrate(
        _sandbox_client(config, env, sub_config),
        AffinityStore(sync_redis),
        sub_config,
    )
    runner = RunnerClient(
        connect_timeout_s=config.runner_connect_timeout_s,
        total_timeout_s=config.runner_total_timeout_s,
    )
    engine = create_async_engine(config.database_url, pool_pre_ping=True)
    binding = BindingResolver(engine, config)
    # One API-lane HTTP client shared by the approval writer (#244) and the two
    # eval-lane reporters below; httpx.AsyncClient is task-safe.
    eval_http = httpx.AsyncClient(timeout=30.0)
    approval_client = ApprovalClient(
        api_base_url=config.api_base_url, api_key=config.api_key, client=eval_http
    )
    kernel = Kernel(
        substrate=substrate,
        runner=runner,
        sink=AsyncSlackSink(
            config.slack_bot_token, base_url=config.slack_api_base_url or None
        ),
        lock=ThreadLock(
            async_redis,
            ttl_ms=config.lock_ttl_ms,
            acquire_timeout_s=config.lock_acquire_timeout_s,
            poll_interval_s=config.lock_poll_interval_s,
        ),
        markers=Markers(async_redis, config),
        config=config,
        binding=binding,
        approvals=approval_client,
        # The same client, handed in twice under the two roles the kernel needs
        # (#1084). Two parameters rather than one so a test can fake the create
        # half without also implementing a read it never exercises.
        approval_reader=approval_client,
        card_store=ApprovalCardStore(async_redis, config),
    )
    killswitch = KillSwitch(async_redis, on_kill=kernel.interrupt_agent)
    kernel.attach_killswitch(killswitch)
    consumer = Consumer(redis=async_redis, kernel=kernel, config=config)

    # The eval lane (F3): a second consumer group on curie:evals, on its own
    # Valkey connection so its blocking read never stalls the runs consumer. It
    # reuses the same substrate (eval runs provision from the same warm pool) and
    # the binding resolver as its repo lookup for the /evals/report payload.
    eval_redis: AsyncRedis = AsyncRedis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
        socket_timeout=config.valkey_socket_timeout_s,
    )
    eval_consumer = EvalStreamConsumer(
        redis=eval_redis,
        config=config,
        bundle_store=BundleStore(config),
        substrate=substrate,
        reporter=EvalReporter(
            api_base_url=config.api_base_url,
            api_key=config.api_key,
            client=eval_http,
            max_attempts=config.report_max_attempts,
            backoff_base_s=config.report_backoff_base_s,
        ),
        recorder=LangfuseEvalRecorder(
            base_url=config.langfuse_host,
            public_key=config.langfuse_public_key,
            secret_key=config.langfuse_secret_key,
            client=eval_http,
        ),
        repo_lookup=binding,
    )
    return Runtime(
        consumer=consumer,
        killswitch=killswitch,
        eval_consumer=eval_consumer,
        runner=runner,
        async_redis=async_redis,
        eval_redis=eval_redis,
        eval_http=eval_http,
        engine=engine,
    )


async def _supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    shutdown: asyncio.Event,
    *,
    restart_backoff_s: float = 1.0,
) -> None:
    """Run a worker task, restarting it if it crashes, until shutdown is requested.

    Each consumer's ``run()`` returns only when its own stop is requested. If one
    instead raises -- a latent bug, or an error that escaped its own read loop --
    restarting it keeps its siblings (runs, evals, killswitch, heartbeat) alive
    rather than letting the exception propagate out of the top-level gather and
    tear the whole worker down (#673). Paired with ``return_exceptions=True`` on
    that gather, this is the defence-in-depth behind the per-entry isolation in
    ``StreamConsumer._consume``. ``CancelledError`` is a ``BaseException`` and
    still propagates, so cooperative shutdown is unaffected.

    ``factory`` is a thunk (e.g. a bound ``run`` method) so each restart gets a
    fresh coroutine; ``run()`` is re-entrant (group creation is BUSYGROUP-safe).
    """
    while not shutdown.is_set():
        try:
            await factory()
            return
        except Exception:
            if shutdown.is_set():
                return
            logger.exception("worker task %s crashed; restarting", name)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=restart_backoff_s)
            except TimeoutError:
                pass


async def _run(config: WorkerConfig, env: Mapping[str, str]) -> None:
    rt = build(config, env)

    loop = asyncio.get_running_loop()

    # A single shutdown flag governs every supervised task. The liveness
    # heartbeat runs on this same event loop, so a wedged loop stops touching the
    # file and the k8s exec probe restarts the pod (issue #71).
    shutdown = asyncio.Event()

    def _stop() -> None:
        rt.consumer.request_stop()
        rt.killswitch.request_stop()
        rt.eval_consumer.request_stop()
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    logging.getLogger("curie_worker").info("worker starting")
    try:
        # return_exceptions=True + per-task restart: a crash in one consumer must
        # not cancel its siblings (#673). Supervisors only return on shutdown.
        await asyncio.gather(
            _supervise("runs", rt.consumer.run, shutdown),
            _supervise("killswitch", rt.killswitch.run, shutdown),
            _supervise("evals", rt.eval_consumer.run, shutdown),
            _supervise(
                "heartbeat",
                lambda: run_heartbeat(
                    config.heartbeat_file, config.heartbeat_interval_s, shutdown
                ),
                shutdown,
            ),
            return_exceptions=True,
        )
    finally:
        await rt.runner.close()
        await rt.eval_http.aclose()
        await rt.async_redis.aclose()
        await rt.eval_redis.aclose()
        await rt.engine.dispose()
    logging.getLogger("curie_worker").info("worker stopped")


def main(env: Mapping[str, str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    install_dead_letter_alerting()
    resolved = env if env is not None else os.environ
    config = WorkerConfig()
    asyncio.run(_run(config, resolved))


if __name__ == "__main__":
    main()
