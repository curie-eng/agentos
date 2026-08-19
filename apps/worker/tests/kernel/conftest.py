"""Harness for the kernel tests.

Real Valkey (compose stack) for stream, locks, and markers; the real G1
``SandboxSubstrate`` with a fake Kubernetes client whose sandboxes resolve to
``127.0.0.1`` so the real ``RunnerClient`` dials an in-process aiohttp fake
runner. The only mocked collaborators are the Slack sink and the model behind the
runner (the fake runner scripts NDJSON frames directly). Async setup is done
inside each test's ``asyncio.run`` body (the repo runs async tests without a
pytest-asyncio plugin), so the reusable pieces here are plain classes plus a few
sync fixtures.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiohttp
import pytest
import redis
from aci_protocol import Final, OutboundEvent, SessionStatus
from aiohttp import web
from aiohttp.test_utils import TestServer
from channel_protocol import OutboundMessage
from curie_test_support.valkey import (
    VALKEY_HOST as _VALKEY_HOST,
)
from curie_test_support.valkey import (
    VALKEY_PORT as _VALKEY_PORT,
)
from curie_test_support.valkey import (
    VALKEY_PW as _VALKEY_PW,
)
from curie_test_support.valkey import (
    connect_or_skip,
)
from curie_worker.approval_cards import ApprovalCardStore
from curie_worker.behaviorpacks import NavPack
from curie_worker.config import WorkerConfig
from curie_worker.kernel import Kernel
from curie_worker.markers import Markers
from curie_worker.runner_client import RunnerClient
from curie_worker.sandbox import (
    AffinityStore,
    QuotaRejection,
    SandboxSubstrate,
    SubstrateConfig,
)
from curie_worker.sandbox.types import ClaimView, SandboxView
from curie_worker.slack_sink import SettledCard, SlackSink
from curie_worker.threadlock import ThreadLock
from redis.asyncio import Redis as AsyncRedis


@pytest.fixture
def sync_redis() -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def names(sync_redis: redis.Redis) -> Iterator[dict[str, str]]:
    """Per-test-unique stream / group / key prefixes on the shared Valkey."""
    token = uuid.uuid4().hex
    ns = {
        "stream": f"test:curie:runs:{token}",
        "group": f"g-{token}",
        "prefix": f"test:curie:worker:{token}",
        "sandbox_prefix": f"test:curie:sandbox:{token}",
    }
    yield ns
    for pat in (f"{ns['prefix']}*", f"{ns['sandbox_prefix']}*", ns["stream"]):
        keys = list(sync_redis.scan_iter(match=pat))
        if keys:
            sync_redis.delete(*keys)


def make_config(names: dict[str, str], **overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "valkey_host": _VALKEY_HOST,
        "valkey_port": _VALKEY_PORT,
        "valkey_password": _VALKEY_PW,
        "stream": names["stream"],
        "consumer_group": names["group"],
        "consumer_name": "test-consumer",
        "key_prefix": names["prefix"],
        "slack_edit_min_interval_s": 0.0,
        "max_attempts": 3,
        "retry_backoff_base_s": 0.001,
        "retry_backoff_max_s": 0.01,
        "lock_ttl_ms": 5000,
        "lock_acquire_timeout_s": 5.0,
        "reclaim_min_idle_ms": 50,
        "reclaim_interval_s": 0.05,
        "read_block_ms": 100,
    }
    base.update(overrides)
    return WorkerConfig(**base)


# --- Fake Slack sink ----------------------------------------------------------


class FakeSink(SlackSink):
    """Records every chat.update and status-clear the kernel makes."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str, str]] = []
        # The nav pack (if any) threaded to each update, parallel to ``updates``.
        self.update_navs: list[NavPack | None] = []
        # The per-turn reply endpoint threaded to each update (issue #19),
        # parallel to ``updates``.
        self.update_endpoints: list[str | None] = []
        self.status_sets: list[tuple[str, str, str]] = []
        self.status_clears: list[tuple[str, str]] = []
        # Sets and clears in ONE ordered log. The two lists above cannot express
        # ordering between a set and a clear, and ordering is the property #1312
        # turns on: both halves now run in this process, so "the caption was
        # raised before it was lowered" has to be assertable, including for a
        # turn that finishes almost instantly.
        self.status_calls: list[tuple[str, str, str]] = []
        # New messages posted by the kernel (the approval card, #246): the
        # channel-neutral OutboundMessage the kernel emits (ADR-0020) plus its
        # framing -- (channel, message, requested_by, thread_ts, endpoint) per
        # post. Block Kit rendering is the adapter's job (see test_slack_sink.py),
        # so this double records the intent, not blocks. The endpoint is the
        # transport the post is delivered through (#451): None means the worker's
        # default Slack transport.
        self.posts: list[
            tuple[str, OutboundMessage, str, str | None, str | None]
        ] = []
        # In-place edits of an already-posted message (settling the approval
        # card: expired in #419, resolved in #1084):
        # (channel, ts, message, endpoint, settled) per update_message. Like
        # posts, the recorded value is the channel-neutral message; the adapter
        # renders the buttonless settled card below the seam. ``settled`` is the
        # semantic outcome and is what distinguishes the two forms.
        self.card_updates: list[
            tuple[str, str, OutboundMessage, str | None, SettledCard | None]
        ] = []
        # #708 test infra: endpoints whose HOST is dead (a CLI stub that exited).
        # A reply-delivery ``update`` to one models the pure-offline local loop --
        # no distinct default transport -- so it RAISES the same aiohttp transport
        # error ``_with_transport_fallback`` re-raises (#530), UNLESS the caller
        # opts into best-effort delivery (``best_effort_unreachable=True``), which
        # logs-and-returns. This is what proves the flag -- not luck -- is what
        # converts a resume turn's dead-letter into a terminal ACK.
        self.dead_endpoints: set[str] = set()
        # Reply deliveries that were swallowed best-effort (channel, ts, text).
        self.swallowed: list[tuple[str, str, str]] = []

    async def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavPack | None = None,
        endpoint: str | None = None,
        best_effort_unreachable: bool = False,
    ) -> None:
        if endpoint is not None and endpoint in self.dead_endpoints:
            if best_effort_unreachable:
                self.swallowed.append((channel, ts, text))
                return
            raise aiohttp.ClientError(f"reply endpoint {endpoint!r} is unreachable")
        self.updates.append((channel, ts, text))
        self.update_navs.append(nav)
        self.update_endpoints.append(endpoint)

    async def set_status(
        self, *, channel: str, thread_ts: str, status: str, endpoint: str | None = None
    ) -> None:
        self.status_sets.append((channel, thread_ts, status))
        self.status_calls.append(("set", thread_ts, status))

    async def clear_status(
        self, *, channel: str, thread_ts: str, endpoint: str | None = None
    ) -> None:
        self.status_clears.append((channel, thread_ts))
        self.status_calls.append(("clear", thread_ts, ""))

    async def post(
        self,
        *,
        channel: str,
        message: OutboundMessage,
        requested_by: str,
        thread_ts: str | None = None,
        endpoint: str | None = None,
    ) -> str | None:
        self.posts.append((channel, message, requested_by, thread_ts, endpoint))
        return f"posted-{len(self.posts)}"

    async def update_message(
        self,
        *,
        channel: str,
        ts: str,
        message: OutboundMessage,
        endpoint: str | None = None,
        settled: SettledCard | None = None,
    ) -> None:
        # ``settled`` is RECORDED, not accepted and dropped (#1084). It carries
        # the whole difference between an expired card and a resolved one, so a
        # fake that swallowed it would let the resolve path regress to rendering
        # an expiry while every assertion in the suite stayed green.
        self.card_updates.append((channel, ts, message, endpoint, settled))

    @property
    def last_text(self) -> str | None:
        return self.updates[-1][2] if self.updates else None

    @property
    def last_nav(self) -> NavPack | None:
        return self.update_navs[-1] if self.update_navs else None


# --- Fake Kubernetes client (sandboxes resolve to 127.0.0.1) ------------------


@dataclass
class _FakeClaim:
    name: str
    sandbox_name: str
    labels: dict[str, str]
    ready: bool = True
    quota_rejection: QuotaRejection | None = None
    ready_reason: str | None = None
    ready_message: str | None = None


@dataclass
class _FakeSandbox:
    name: str
    operating_mode: str = "Running"


@dataclass
class FakeK8s:
    """In-memory agent-sandbox model whose sandboxes dial the local fake runner."""

    namespace: str = "test-ns"
    claims: dict[str, _FakeClaim] = field(default_factory=dict)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)
    claim_envs: list[dict[str, str] | None] = field(default_factory=list)
    bind_ready: bool = True
    quota_rejection: QuotaRejection | None = None
    ready_reason: str | None = None
    ready_message: str | None = None

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.claim_envs.append(env)
        sandbox_name = f"sbx-{name}"
        self.claims[name] = _FakeClaim(
            name=name,
            sandbox_name=sandbox_name,
            labels={"curietech.ai/managed-by": "curie-sandbox-substrate", **(labels or {})},
            ready=self.bind_ready and self.quota_rejection is None,
            quota_rejection=self.quota_rejection,
            ready_reason=self.ready_reason,
            ready_message=self.ready_message,
        )
        self.sandboxes[sandbox_name] = _FakeSandbox(name=sandbox_name)

    def get_claim(self, name: str) -> ClaimView | None:
        claim = self.claims.get(name)
        if claim is None:
            return None
        return ClaimView(
            name=claim.name,
            ready=claim.ready,
            sandbox_name=claim.sandbox_name if claim.ready else None,
            # This suite never reaps; the claim is simply as new as it looks.
            created_at=datetime.now(UTC),
            quota_rejection=claim.quota_rejection,
            ready_reason=claim.ready_reason,
            ready_message=claim.ready_message,
        )

    def delete_claim(self, name: str) -> None:
        claim = self.claims.pop(name, None)
        if claim is not None:
            self.sandboxes.pop(claim.sandbox_name, None)

    def list_claims(self, *, label_selector: str) -> list[ClaimView]:
        key, _, value = label_selector.partition("=")
        out = []
        for claim in self.claims.values():
            if claim.labels.get(key) == value:
                view = self.get_claim(claim.name)
                assert view is not None
                out.append(view)
        return out

    def get_sandbox(self, name: str) -> SandboxView | None:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            return None
        # 127.0.0.1 so RunnerClient reaches the in-process fake runner.
        return SandboxView(
            name=sandbox.name,
            ready=True,
            service_fqdn="127.0.0.1",
            operating_mode=sandbox.operating_mode,
        )

    def set_sandbox_mode(self, name: str, mode: str) -> None:
        self.sandboxes[name].operating_mode = mode


# --- Fake runner (aiohttp) ----------------------------------------------------


class FakeRunner:
    """A scriptable ACI runner over HTTP.

    ``turn_scripts`` is a FIFO of frame lists, one consumed per ``/v1/event``;
    when exhausted it falls back to ``default_script``. A script that omits a
    ``final`` and ends the stream models a mid-run drop (no terminal). ``hold``
    (an asyncio.Event set by the test or by ``/v1/interrupt``) makes a turn hang
    active after its prefix so steer/interrupt can be exercised against a live
    turn; ``tail`` frames flush on release.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/status", self._status),
                web.post("/v1/event", self._event),
                web.post("/v1/steer", self._steer),
                web.post("/v1/interrupt", self._interrupt),
            ]
        )
        self.turn_active = False
        self.turn_scripts: list[list[OutboundEvent]] = []
        self.default_script: list[OutboundEvent] = [Final(text="ok", status=SessionStatus.DONE)]
        self.opened: list[str] = []
        self.steers: list[str] = []
        self.interrupts: int = 0
        self.hold: object | None = None  # asyncio.Event when a turn should hang
        self.tail: list[OutboundEvent] = []
        self.event_fail_times: int = 0  # return 500 on the next N /v1/event calls
        # Per-route captured request headers (the ACI auth Bearer check reads
        # these): the most recent request's headers for each route, so a test can
        # assert the worker delivered its per-sandbox token as Authorization.
        self.event_headers: list[dict[str, str]] = []
        self.steer_headers: list[dict[str, str]] = []
        self.interrupt_headers: list[dict[str, str]] = []
        self.status_headers: list[dict[str, str]] = []

    async def _status(self, request: web.Request) -> web.Response:
        self.status_headers.append(dict(request.headers))
        return web.json_response({"status": "idle-awaiting-input", "turn_active": self.turn_active})

    async def _event(self, request: web.Request) -> web.StreamResponse:
        self.event_headers.append(dict(request.headers))
        body = await request.json()
        self.opened.append(body["text"])
        if self.event_fail_times > 0:
            self.event_fail_times -= 1
            return web.json_response({"error": "transient runner failure"}, status=500)
        script = self.turn_scripts.pop(0) if self.turn_scripts else list(self.default_script)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        self.turn_active = True
        for frame in script:
            await resp.write((frame.model_dump_json() + "\n").encode("utf-8"))
        if self.hold is not None:
            await self.hold.wait()  # type: ignore[attr-defined]
            for frame in self.tail:
                await resp.write((frame.model_dump_json() + "\n").encode("utf-8"))
        self.turn_active = False
        await resp.write_eof()
        return resp

    async def _steer(self, request: web.Request) -> web.Response:
        self.steer_headers.append(dict(request.headers))
        body = await request.json()
        if not self.turn_active:
            return web.json_response({"error": "no active turn"}, status=409)
        self.steers.append(body["text"])
        return web.json_response({"ok": True})

    async def _interrupt(self, request: web.Request) -> web.Response:
        self.interrupt_headers.append(dict(request.headers))
        self.interrupts += 1
        if self.hold is not None:
            self.hold.set()  # type: ignore[attr-defined]
        return web.json_response({"ok": True})


@dataclass
class Harness:
    substrate: SandboxSubstrate
    kernel: Kernel
    sink: FakeSink
    runner: FakeRunner
    config: WorkerConfig
    async_redis: AsyncRedis
    fake_k8s: FakeK8s
    card_store: ApprovalCardStore
    killswitch: object | None = None


@pytest.fixture
def make_harness(
    names: dict[str, str], sync_redis: redis.Redis
) -> Callable[..., contextlib.AbstractAsyncContextManager[Harness]]:
    """A factory the tests call inside their asyncio.run body: ``make_harness()``.

    Closes over the per-test names and the sync Valkey client so tests need no
    conftest import (which importlib mode makes fragile). Optional kwargs:
    ``binding`` (a resolver injected into the kernel) and ``with_killswitch``
    (build a real KillSwitch wired to the kernel); the rest are config overrides."""

    def factory(**overrides: object) -> contextlib.AbstractAsyncContextManager[Harness]:
        return kernel_harness(names, sync_redis, **overrides)

    return factory


@contextlib.asynccontextmanager
async def kernel_harness(
    names: dict[str, str],
    sync_redis: redis.Redis,
    *,
    binding: object | None = None,
    with_killswitch: bool = False,
    approvals: object | None = None,
    approval_reader: object | None = None,
    claim_timeout_seconds: float = 3.0,
    **config_overrides: object,
) -> AsyncIterator[Harness]:
    """Assemble a live kernel wired to a fake runner and real Valkey."""
    config = make_config(names, **config_overrides)
    fake_runner = FakeRunner()
    server = TestServer(fake_runner.app)
    await server.start_server()
    port = server.port
    assert port is not None

    fake_k8s = FakeK8s()
    substrate = SandboxSubstrate(
        fake_k8s,  # type: ignore[arg-type]
        AffinityStore(sync_redis, key_prefix=names["sandbox_prefix"]),
        SubstrateConfig(
            namespace="test-ns",
            warm_pool="test-pool",
            runner_port=port,
            route_ttl_seconds=60,
            claim_timeout_seconds=claim_timeout_seconds,
            poll_interval_seconds=0.005,
            key_prefix=names["sandbox_prefix"],
        ),
    )
    async_redis: AsyncRedis = AsyncRedis(
        host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None, decode_responses=True
    )
    sink = FakeSink()
    runner_client = RunnerClient(total_timeout_s=30.0)
    card_store = ApprovalCardStore(async_redis, config)
    kernel = Kernel(
        substrate=substrate,
        runner=runner_client,
        sink=sink,
        lock=ThreadLock(
            async_redis,
            ttl_ms=config.lock_ttl_ms,
            acquire_timeout_s=config.lock_acquire_timeout_s,
            poll_interval_s=config.lock_poll_interval_s,
        ),
        markers=Markers(async_redis, config),
        config=config,
        binding=binding,  # type: ignore[arg-type]
        approvals=approvals,  # type: ignore[arg-type]
        approval_reader=approval_reader,  # type: ignore[arg-type]
        card_store=card_store,
    )
    killswitch = None
    if with_killswitch:
        from curie_worker.killswitch import KillSwitch

        killswitch = KillSwitch(async_redis, on_kill=kernel.interrupt_agent)
        kernel.attach_killswitch(killswitch)
    try:
        yield Harness(
            substrate,
            kernel,
            sink,
            fake_runner,
            config,
            async_redis,
            fake_k8s,
            card_store,
            killswitch,
        )
    finally:
        with contextlib.suppress(Exception):
            await runner_client.close()
        with contextlib.suppress(Exception):
            await async_redis.aclose()
        with contextlib.suppress(Exception):
            await server.close()
