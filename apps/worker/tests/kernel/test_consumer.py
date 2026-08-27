"""Consumer tests: end-to-end stream consumption and crash-recovery reclaim,
against the real Valkey stream + consumer group.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import redis.exceptions
from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from curie_dispatcher.queue import to_stream_fields
from curie_worker import consumer as consumer_module
from curie_worker import kernel as kernel_module
from curie_worker.consumer import (
    THREAD_RESET_INFLIGHT_SET,
    THREAD_RESET_SET,
    Consumer,
)
from curie_worker.consumer_liveness import (
    ConsumerLivenessStore,
    consumer_heartbeat_capable_key,
    consumer_heartbeat_key,
)
from curie_worker.sandbox import QuotaRejection
from curie_worker.stream_consumer import ConsumerLivenessExpired
from redis.asyncio import Redis as AsyncRedis

DONE = SessionStatus.DONE

HEARTBEAT_TTL_MS = 15_000


async def _wait_consumer_idle(
    redis: AsyncRedis, stream: str, group: str, consumer: str, idle_ms: int
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = await redis.xinfo_consumers(stream, group)
        if any(
            str(row["name"]) == consumer and int(row.get("idle") or 0) >= idle_ms for row in rows
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("consumer did not become idle")


async def _deliveries(redis: AsyncRedis, stream: str, group: str) -> dict[str, int]:
    rows = await redis.xpending_range(stream, group, min="-", max="+", count=100)
    return {str(row["message_id"]): int(row["times_delivered"]) for row in rows}


async def _wait_key(redis: AsyncRedis, key: str, *, present: bool = True) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if bool(await redis.exists(key)) is present:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"key {key!r} did not become {'present' if present else 'absent'}")


async def _pending_owner(redis: AsyncRedis, stream: str, group: str, entry_id: str) -> str | None:
    rows = await redis.xpending_range(stream, group, min=entry_id, max=entry_id, count=1)
    return str(rows[0]["consumer"]) if rows else None


class _RenewalProbeStore:
    """Fault injector around the real liveness adapter, never around Valkey."""

    def __init__(
        self,
        delegate: ConsumerLivenessStore,
        *,
        fail_renewals: int = 0,
        timeout_renewals: int = 0,
        hang_renewals: bool = False,
    ) -> None:
        self._delegate = delegate
        self._fail_renewals = fail_renewals
        self._timeout_renewals = timeout_renewals
        self._hang_renewals = hang_renewals
        self.renew_calls = 0
        self._never = asyncio.Event()

    async def publish(self, **kwargs: Any) -> None:
        await self._delegate.publish(**kwargs)

    async def renew(self, **kwargs: Any) -> None:
        self.renew_calls += 1
        if self.renew_calls <= self._fail_renewals:
            raise redis.exceptions.ConnectionError("injected transient renewal failure")
        if self.renew_calls <= self._fail_renewals + self._timeout_renewals:
            await self._never.wait()
        if self._hang_renewals:
            await self._never.wait()
        await self._delegate.renew(**kwargs)

    async def is_alive(self, **kwargs: Any) -> bool:
        return await self._delegate.is_alive(**kwargs)

    async def is_capable(self, **kwargs: Any) -> bool:
        return await self._delegate.is_capable(**kwargs)

    async def cleanup_alive(self, **kwargs: Any) -> None:
        await self._delegate.cleanup_alive(**kwargs)

    async def try_acquire_reclaim(self, **kwargs: Any) -> str | None:
        return await self._delegate.try_acquire_reclaim(**kwargs)

    async def release_reclaim(self, **kwargs: Any) -> None:
        await self._delegate.release_reclaim(**kwargs)


def _qevent(text: str, *, thread: str = "th-1", event_id: str | None = None) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(kind="slack", channel="C1", placeholder="p-1"),
        received_at="2026-07-05T00:00:00+00:00",
    )


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_consumes_stream_entry_end_to_end_and_acks(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [TextDelta(text="hi "), Final(text="answer", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("hello", thread="tc1", event_id="c1")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            task = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.sink.last_text == "answer")
            consumer.request_stop()
            await task

            assert h.runner.opened == ["hello"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0  # the entry was acked

    asyncio.run(go())


def test_reclaim_skips_this_consumers_own_inflight_entry(make_harness) -> None:
    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            # A turn that hangs, so its stream entry stays pending (unacked, in
            # flight) while streaming.
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="done", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("hello", thread="ti1", event_id="i1")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            task = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.runner.turn_active)

            # A reclaim pass while the turn is still in flight must NOT re-dispatch
            # our own entry (which would steer the same prompt into its own turn).
            reclaimed = await consumer._reclaim_once()
            assert reclaimed == 0
            assert h.runner.opened == ["hello"]  # no duplicate turn

            hold.set()
            await _wait_until(lambda: h.sink.last_text == "done")
            consumer.request_stop()
            await task

    asyncio.run(go())


def test_dispatch_applies_backpressure_at_capacity(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # A hanging turn holds the single capacity slot; the next dispatch must
            # block (backpressure) rather than claim the entry into a local queue.
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="w")]
            h.runner.tail = [Final(text="done", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis, kernel=h.kernel, config=h.config, max_concurrency=1
            )
            await consumer.ensure_group()

            first = to_stream_fields(_qevent("a", thread="ta", event_id="a"))
            await consumer._dispatch("1-0", first)
            await _wait_until(lambda: h.runner.turn_active)  # slot taken, turn hanging

            second_fields = to_stream_fields(_qevent("b", thread="tb", event_id="b"))
            second = asyncio.create_task(consumer._dispatch("2-0", second_fields))
            await asyncio.sleep(0.1)
            assert not second.done()  # blocked: capacity is full

            hold.set()  # first turn finishes, frees the slot
            await second  # second dispatch now proceeds
            await asyncio.gather(*list(consumer._inflight))

    asyncio.run(go())


def test_ensure_group_does_not_replay_preexisting_backlog(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # A stale entry already on the stream BEFORE the group is created (a
            # persistent Valkey carrying a backlog from a prior deploy). Creating
            # the group at "$" must skip it; creating at "0" would storm it.
            stale = _qevent("stale", thread="tb1", event_id="b1")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(stale))

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            # An entry produced AFTER the group exists must still be delivered.
            fresh = _qevent("fresh", thread="tb2", event_id="b2")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(fresh))
            h.runner.default_script = [Final(text="answer", status=DONE)]

            task = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.sink.last_text == "answer")
            consumer.request_stop()
            await task

            # Only the post-group entry ran; the stale backlog was never opened.
            assert h.runner.opened == ["fresh"]

    asyncio.run(go())


def test_read_loop_survives_transient_redis_timeout(make_harness, caplog) -> None:
    async def go() -> None:
        async with make_harness() as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            # The first blocking read raises a transient redis TimeoutError (the
            # routine idle case) and the second a ConnectionError (a real fault).
            # The loop must survive both and process the next read; an unguarded
            # read would kill the worker. The two are logged at different levels:
            # an idle timeout is DEBUG (not log-worthy every idle interval), a
            # connection blip stays WARNING.
            real = h.async_redis.xreadgroup
            calls = {"n": 0}

            async def flaky(*args: object, **kwargs: object) -> object:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise redis.exceptions.TimeoutError("simulated blocking-read timeout")
                if calls["n"] == 2:
                    raise redis.exceptions.ConnectionError("simulated connection blip")
                return await real(*args, **kwargs)

            consumer._redis.xreadgroup = flaky  # type: ignore[method-assign,assignment]

            h.runner.default_script = [Final(text="answer", status=DONE)]
            qe = _qevent("hello", thread="tt1", event_id="t1")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            with caplog.at_level(logging.DEBUG, logger="curie_worker.consumer"):
                task = asyncio.create_task(consumer.run())
                await _wait_until(lambda: h.sink.last_text == "answer")
                consumer.request_stop()
                await task

            assert calls["n"] >= 3  # it retried after both injected faults
            assert h.runner.opened == ["hello"]

            recs = [r for r in caplog.records if r.name == "curie_worker.consumer"]
            timeout_recs = [r for r in recs if "simulated blocking-read timeout" in r.getMessage()]
            conn_recs = [r for r in recs if "simulated connection blip" in r.getMessage()]
            assert timeout_recs and all(r.levelno == logging.DEBUG for r in timeout_recs)
            assert conn_recs and all(r.levelno == logging.WARNING for r in conn_recs)

    asyncio.run(go())


def test_reclaims_and_reprocesses_a_dead_consumers_pending_entry(make_harness) -> None:
    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("orphan", thread="tr1", event_id="r1")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            # A different (now "dead") consumer takes delivery but never acks,
            # leaving the entry pending — the crash mid-run case.
            dead = await h.async_redis.xreadgroup(
                h.config.consumer_group, "dead-consumer", {h.config.stream: ">"}, count=1
            )
            assert dead

            # Our consumer reclaims the pending entry and reprocesses it.
            reclaimed = await consumer._reclaim_once()
            assert reclaimed == 1
            await _wait_until(lambda: h.sink.last_text == "recovered")
            await asyncio.gather(*list(consumer._inflight))

            assert h.runner.opened == ["orphan"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0  # reclaimed entry acked after reprocessing

    asyncio.run(go())


def test_reclaims_a_dead_consumers_pending_entry_without_waiting_min_idle(
    make_harness,
) -> None:
    """#1532 extension: a terminated consumer's pending entry is recovered
    promptly. ``reclaim_min_idle_ms`` stays at the 15-minute production default
    so XAUTOCLAIM cannot be the path that succeeds; recovery must come from the
    dead-consumer idle check instead.
    """

    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("orphan", thread="tr-dead", event_id="r-dead")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            dead = await h.async_redis.xreadgroup(
                h.config.consumer_group, "dead-consumer", {h.config.stream: ">"}, count=1
            )
            assert dead
            store = ConsumerLivenessStore(h.async_redis)
            await store.publish(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="dead-consumer",
                heartbeat_ttl_ms=1,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            capable_key = consumer_heartbeat_capable_key(
                h.config.stream, h.config.consumer_group, "dead-consumer"
            )
            key = consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "dead-consumer")
            await _wait_key(h.async_redis, key, present=False)
            assert not await h.async_redis.exists(key)
            await _wait_consumer_idle(
                h.async_redis,
                h.config.stream,
                h.config.consumer_group,
                "dead-consumer",
                h.config.dead_consumer_idle_ms,
            )
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 1

            # One missing lease can be a Valkey blip; prompt reclaim requires a
            # second absence at least one complete heartbeat TTL later.
            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            pending = await h.async_redis.xpending_range(
                h.config.stream, h.config.consumer_group, min="-", max="+", count=1
            )
            assert int(pending[0]["time_since_delivered"]) < h.config.reclaim_min_idle_ms
            reclaimed = await consumer._prompt_reclaim_once()
            assert reclaimed == 1
            await _wait_until(lambda: h.sink.last_text == "recovered")
            await asyncio.gather(*list(consumer._inflight))
            assert h.runner.opened == ["orphan"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0
            await h.async_redis.delete(capable_key)

    asyncio.run(go())


def test_prompt_reclaim_arbitrates_across_replicas_without_burning_delivery_budget(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="reclaimed")]
            h.runner.tail = [Final(text="done", status=DONE)]
            first_config = h.config.model_copy(update={"consumer_name": "replacement-a"})
            second_config = h.config.model_copy(update={"consumer_name": "replacement-b"})
            first = Consumer(redis=h.async_redis, kernel=h.kernel, config=first_config)
            second = Consumer(redis=h.async_redis, kernel=h.kernel, config=second_config)
            await first.ensure_group()

            entry_id = await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("race", thread="tr-race", event_id="r-race")),
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group,
                "dead-race-peer",
                {h.config.stream: ">"},
                count=1,
            )
            await h.async_redis.set(
                consumer_heartbeat_capable_key(
                    h.config.stream, h.config.consumer_group, "dead-race-peer"
                ),
                "1",
                px=h.config.consumer_capability_ttl_ms,
            )

            assert await asyncio.gather(
                first._prompt_reclaim_once(), second._prompt_reclaim_once()
            ) == [0, 0]
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            results = await asyncio.gather(
                first._prompt_reclaim_once(), second._prompt_reclaim_once()
            )
            assert sum(results) == 1
            await _wait_until(lambda: h.runner.turn_active)

            rows = await h.async_redis.xpending_range(
                h.config.stream,
                h.config.consumer_group,
                min=entry_id,
                max=entry_id,
                count=1,
            )
            assert len(rows) == 1
            assert rows[0]["consumer"] in {"replacement-a", "replacement-b"}
            assert int(rows[0]["times_delivered"]) == 2
            assert h.runner.opened == ["race"]

            hold.set()
            await asyncio.gather(*list(first._inflight | second._inflight))
            assert (await h.async_redis.xpending(h.config.stream, h.config.consumer_group))[
                "pending"
            ] == 0

    asyncio.run(go())


def test_local_generation_bootstrap_contends_with_peer_transfer_lease(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            owner_config = h.config.model_copy(update={"consumer_name": "restart-owner"})
            peer_config = h.config.model_copy(update={"consumer_name": "replacement-peer"})
            owner = Consumer(redis=h.async_redis, kernel=h.kernel, config=owner_config)
            peer = Consumer(redis=h.async_redis, kernel=h.kernel, config=peer_config)
            owner._liveness_store = ConsumerLivenessStore(h.async_redis)
            peer._liveness_store = ConsumerLivenessStore(h.async_redis)
            await owner.ensure_group()

            entry_id = await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(
                    _qevent("bootstrap-race", thread="tr-bootstrap", event_id="r-bootstrap")
                ),
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group,
                owner_config.consumer_name,
                {h.config.stream: ">"},
                count=1,
            )

            # Model a peer that has already won the transfer lease while the
            # stable-name consumer starts its replacement generation.
            token = await peer._liveness_store.try_acquire_reclaim(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer=owner_config.consumer_name,
                ttl_ms=60_000,
            )
            assert token is not None
            bootstrap = asyncio.create_task(owner._recover_local_pending_once())
            try:
                await asyncio.sleep(0.03)
                assert not bootstrap.done()
                rows = await h.async_redis.xpending_range(
                    h.config.stream,
                    h.config.consumer_group,
                    min=entry_id,
                    max=entry_id,
                    count=1,
                )
                assert len(rows) == 1
                assert rows[0]["consumer"] == owner_config.consumer_name
                assert int(rows[0]["times_delivered"]) == 1
                assert h.runner.opened == []

                async with peer._reclaim_lock:
                    entries = await peer._claim_consumer_pending_locked(
                        owner_config.consumer_name, set()
                    )
                assert await peer._dispatch_reclaimed(entries) == 1
            finally:
                await peer._liveness_store.release_reclaim(
                    stream=h.config.stream,
                    group=h.config.consumer_group,
                    consumer=owner_config.consumer_name,
                    token=token,
                )

            await asyncio.wait_for(bootstrap, timeout=1)
            await _wait_until(lambda: h.runner.opened == ["bootstrap-race"])
            await asyncio.gather(*list(peer._inflight))
            assert (await h.async_redis.xpending(h.config.stream, h.config.consumer_group))[
                "pending"
            ] == 0

    asyncio.run(go())


def test_reclaim_does_not_promptly_steal_from_unknown_peer_without_heartbeat_capability(
    make_harness,
) -> None:
    """A pre-marker worker stays on the long XAUTOCLAIM backstop."""

    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("unknown", thread="tr-unknown", event_id="r-unknown")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            claimed = await h.async_redis.xreadgroup(
                h.config.consumer_group, "unknown-peer", {h.config.stream: ">"}, count=1
            )
            assert claimed
            capable_key = consumer_heartbeat_capable_key(
                h.config.stream, h.config.consumer_group, "unknown-peer"
            )
            heartbeat_key = consumer_heartbeat_key(
                h.config.stream, h.config.consumer_group, "unknown-peer"
            )
            assert not await h.async_redis.exists(capable_key)
            assert not await h.async_redis.exists(heartbeat_key)
            await _wait_consumer_idle(
                h.async_redis,
                h.config.stream,
                h.config.consumer_group,
                "unknown-peer",
                h.config.dead_consumer_idle_ms,
            )
            before = await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
            assert before == {entry_id: 1}

            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await consumer._prompt_reclaim_once() == 0
            assert (
                await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group) == before
            )
            pending = await h.async_redis.xpending_range(
                h.config.stream,
                h.config.consumer_group,
                min="-",
                max="+",
                count=10,
                consumername="unknown-peer",
            )
            assert [str(row["message_id"]) for row in pending] == [entry_id]
            assert h.runner.opened == []

    asyncio.run(go())


def test_reclaim_does_not_steal_from_a_fresh_live_peer_when_min_idle_is_high(
    make_harness,
) -> None:
    """A live overlapping replica (rolling update) still has a near-zero
    consumer idle because its read loop keeps issuing XREADGROUP. The
    dead-consumer path must not steal that replica's in-flight entry just
    because the entry itself is already pending.
    """

    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("live", thread="tr-live", event_id="r-live")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            live = await h.async_redis.xreadgroup(
                h.config.consumer_group, "live-peer", {h.config.stream: ">"}, count=1
            )
            assert live
            capable_key = consumer_heartbeat_capable_key(
                h.config.stream, h.config.consumer_group, "live-peer"
            )
            await h.async_redis.set(capable_key, "1")
            key = consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "live-peer")
            await h.async_redis.set(key, "alive", px=HEARTBEAT_TTL_MS)
            await _wait_consumer_idle(
                h.async_redis,
                h.config.stream,
                h.config.consumer_group,
                "live-peer",
                h.config.dead_consumer_idle_ms,
            )
            before = await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            reclaimed = await consumer._prompt_reclaim_once()
            assert reclaimed == 0
            assert (
                await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group) == before
            )
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 1
            assert h.runner.opened == []
            await h.async_redis.delete(key, capable_key)

    asyncio.run(go())


def test_reclaim_does_not_steal_from_a_live_saturated_peer(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            ids = []
            for text in ("busy", "queued"):
                ids.append(
                    await h.async_redis.xadd(h.config.stream, to_stream_fields(_qevent(text)))
                )
            claimed = await h.async_redis.xreadgroup(
                h.config.consumer_group, "saturated-peer", {h.config.stream: ">"}, count=2
            )
            assert claimed
            capable_key = consumer_heartbeat_capable_key(
                h.config.stream, h.config.consumer_group, "saturated-peer"
            )
            await h.async_redis.set(capable_key, "1")
            key = consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "saturated-peer")
            await h.async_redis.set(key, "alive", px=HEARTBEAT_TTL_MS)
            await _wait_consumer_idle(
                h.async_redis,
                h.config.stream,
                h.config.consumer_group,
                "saturated-peer",
                h.config.dead_consumer_idle_ms,
            )
            before = await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
            assert set(before) == set(ids)
            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await consumer._prompt_reclaim_once() == 0
            assert (
                await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group) == before
            )
            assert h.runner.opened == []
            await h.async_redis.delete(key, capable_key)

    asyncio.run(go())


def test_consumer_publishes_before_reads_and_renews_alive_and_capability(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=300,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=3000,
            read_block_ms=10,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            alive = consumer_heartbeat_key(
                h.config.stream, h.config.consumer_group, h.config.consumer_name
            )
            capable = consumer_heartbeat_capable_key(
                h.config.stream, h.config.consumer_group, h.config.consumer_name
            )
            read_observations: list[tuple[bool, bool]] = []
            real_read = consumer._redis.xreadgroup

            async def observe_first_read(*args: Any, **kwargs: Any) -> Any:
                read_observations.append(
                    (
                        bool(await h.async_redis.exists(alive)),
                        bool(await h.async_redis.exists(capable)),
                    )
                )
                return await real_read(*args, **kwargs)

            consumer._redis.xreadgroup = observe_first_read  # type: ignore[method-assign,assignment]

            task = asyncio.create_task(consumer.run())
            await _wait_key(h.async_redis, alive)
            await _wait_key(h.async_redis, capable)
            # Live longer than the capability marker's original TTL. Both keys
            # surviving proves the refresher renews capability as well as alive.
            await asyncio.sleep(0.55)
            assert await h.async_redis.pttl(alive) > 0
            assert await h.async_redis.pttl(capable) > 0
            assert read_observations and read_observations[0] == (True, True)

            consumer.request_stop()
            await task
            assert not await h.async_redis.exists(alive)
            assert await h.async_redis.exists(capable)

    asyncio.run(go())


def test_graceful_stop_keeps_lease_through_two_ttls_of_inflight_drain(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=300,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=450,
            read_block_ms=10,
        ) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="done", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("drain", thread="th-drain", event_id="drain")),
            )
            alive = consumer_heartbeat_key(
                h.config.stream, h.config.consumer_group, h.config.consumer_name
            )

            task = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.runner.turn_active)
            await _wait_key(h.async_redis, alive)
            consumer.request_stop()
            await asyncio.sleep(0.32)
            assert await h.async_redis.exists(alive), (
                "graceful stop dropped liveness while the owned handler still drained"
            )
            assert not task.done()

            hold.set()
            await task
            assert not await h.async_redis.exists(alive)

    asyncio.run(go())


def test_real_saturated_consumer_renews_lease_and_peer_cannot_prompt_claim(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=6000,
            read_block_ms=10,
        ) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="busy")]
            h.runner.tail = [Final(text="done", status=DONE)]
            peer = Consumer(
                redis=h.async_redis, kernel=h.kernel, config=h.config, max_concurrency=1
            )
            await peer.ensure_group()
            ids = [
                await h.async_redis.xadd(
                    h.config.stream,
                    to_stream_fields(_qevent(text, event_id=f"sat-{text}")),
                )
                for text in ("held", "semaphore-blocked")
            ]
            alive = consumer_heartbeat_key(
                h.config.stream, h.config.consumer_group, h.config.consumer_name
            )

            peer_task = asyncio.create_task(peer.run())
            await _wait_until(lambda: h.runner.turn_active)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if set(
                    await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
                ) == set(ids):
                    break
                await asyncio.sleep(0.005)
            before = await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
            assert before == {ids[0]: 1, ids[1]: 1}
            await asyncio.sleep(0.32)
            assert await h.async_redis.exists(alive)

            replacement_config = h.config.model_copy(update={"consumer_name": "replacement"})
            replacement = Consumer(redis=h.async_redis, kernel=h.kernel, config=replacement_config)
            assert await replacement._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await replacement._prompt_reclaim_once() == 0
            assert (
                await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group) == before
            )

            peer.request_stop()
            hold.set()
            await peer_task

    asyncio.run(go())


def test_alive_restoration_resets_two_absence_proof(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            entry_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("restored", event_id="restored"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "peer", {h.config.stream: ">"}, count=1
            )
            store = ConsumerLivenessStore(h.async_redis)
            await store.publish(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="peer",
                heartbeat_ttl_ms=1,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            await _wait_key(
                h.async_redis,
                consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "peer"),
                present=False,
            )
            assert await consumer._prompt_reclaim_once() == 0

            await store.renew(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="peer",
                heartbeat_ttl_ms=100,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await consumer._prompt_reclaim_once() == 0
            assert "peer" not in consumer._peer_absent_since
            assert (
                await _pending_owner(
                    h.async_redis, h.config.stream, h.config.consumer_group, entry_id
                )
                == "peer"
            )

            await h.async_redis.delete(
                consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "peer")
            )
            assert await consumer._prompt_reclaim_once() == 0
            assert (
                await _pending_owner(
                    h.async_redis, h.config.stream, h.config.consumer_group, entry_id
                )
                == "peer"
            )

    asyncio.run(go())


def test_disappeared_consumer_invalidates_first_absence_observation(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            entry_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("gone", event_id="gone"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "vanished-peer", {h.config.stream: ">"}, count=1
            )
            await h.async_redis.set(
                consumer_heartbeat_capable_key(
                    h.config.stream, h.config.consumer_group, "vanished-peer"
                ),
                "1",
                px=h.config.consumer_capability_ttl_ms,
            )
            assert await consumer._prompt_reclaim_once() == 0
            assert "vanished-peer" in consumer._peer_absent_since

            await h.async_redis.xack(h.config.stream, h.config.consumer_group, entry_id)
            await h.async_redis.xgroup_delconsumer(
                h.config.stream, h.config.consumer_group, "vanished-peer"
            )
            assert await consumer._prompt_reclaim_once() == 0
            assert "vanished-peer" not in consumer._peer_absent_since

    asyncio.run(go())


def test_live_at_cap_peer_is_not_dead_lettered_by_prompt_path(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            max_delivery=2,
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            entry_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("live-cap", event_id="live-cap"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "live-cap-peer", {h.config.stream: ">"}, count=1
            )
            await h.async_redis.xclaim(
                h.config.stream,
                h.config.consumer_group,
                "live-cap-peer",
                0,
                [entry_id],
            )
            store = ConsumerLivenessStore(h.async_redis)
            await store.publish(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="live-cap-peer",
                heartbeat_ttl_ms=100,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            before = await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group)
            assert before == {entry_id: 2}
            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await consumer._prompt_reclaim_once() == 0
            assert (
                await _deliveries(h.async_redis, h.config.stream, h.config.consumer_group) == before
            )
            assert await h.async_redis.xlen(h.config.dead_letter_stream_name()) == 0

    asyncio.run(go())


def test_proven_dead_at_cap_peer_is_dead_lettered_without_xclaim(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            max_delivery=2,
            reclaim_min_idle_ms=5000,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=6000,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            entry_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("dead-cap", event_id="dead-cap"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "dead-cap-peer", {h.config.stream: ">"}, count=1
            )
            await h.async_redis.xclaim(
                h.config.stream,
                h.config.consumer_group,
                "dead-cap-peer",
                0,
                [entry_id],
            )
            store = ConsumerLivenessStore(h.async_redis)
            await store.publish(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="dead-cap-peer",
                heartbeat_ttl_ms=1,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            await _wait_key(
                h.async_redis,
                consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "dead-cap-peer"),
                present=False,
            )
            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            assert await consumer._prompt_reclaim_once() == 0

            assert (await h.async_redis.xpending(h.config.stream, h.config.consumer_group))[
                "pending"
            ] == 0
            rows = await h.async_redis.xrange(h.config.dead_letter_stream_name())
            assert len(rows) == 1
            assert rows[0][1]["dl_original_id"] == entry_id
            # A prompt XCLAIM would increment this to 3. Direct dead-lettering
            # preserves the durable count at the configured cap.
            assert rows[0][1]["dl_delivery_count"] == "2"
            assert h.runner.opened == []

    asyncio.run(go())


def test_transient_liveness_renewal_failure_recovers_before_lease_expiry(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=300,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=450,
            read_block_ms=10,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            probe = _RenewalProbeStore(ConsumerLivenessStore(h.async_redis), fail_renewals=1)
            consumer._liveness_store = probe  # type: ignore[assignment]
            alive = consumer_heartbeat_key(
                h.config.stream, h.config.consumer_group, h.config.consumer_name
            )

            task = asyncio.create_task(consumer.run())
            deadline = time.monotonic() + 2
            while probe.renew_calls < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert probe.renew_calls >= 2
            assert not task.done()
            assert await h.async_redis.exists(alive)

            consumer.request_stop()
            await task

    asyncio.run(go())


def test_timed_out_liveness_renewal_retries_before_lease_expiry(make_harness) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=300,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=450,
            read_block_ms=10,
        ) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            probe = _RenewalProbeStore(
                ConsumerLivenessStore(h.async_redis), timeout_renewals=1
            )
            consumer._liveness_store = probe  # type: ignore[assignment]

            task = asyncio.create_task(consumer.run())
            deadline = time.monotonic() + 2
            while probe.renew_calls < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert probe.renew_calls >= 2
            assert not task.done()

            consumer.request_stop()
            await task

    asyncio.run(go())


def test_terminal_liveness_failure_cancels_generation_and_clean_restart_recovers_pel(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=300,
            reclaim_interval_s=0.02,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=150,
            consumer_capability_ttl_ms=3000,
            read_block_ms=10,
        ) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="started")]
            h.runner.tail = [Final(text="recovered", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis, kernel=h.kernel, config=h.config, max_concurrency=1
            )
            consumer._liveness_store = _RenewalProbeStore(  # type: ignore[assignment]
                ConsumerLivenessStore(h.async_redis), hang_renewals=True
            )
            await consumer.ensure_group()
            entry_id = await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("restart", thread="th-restart", event_id="restart")),
            )

            first = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.runner.turn_active)
            consumer._peer_absent_since["stale-generation-peer"] = time.monotonic()
            with pytest.raises(ConsumerLivenessExpired):
                await asyncio.wait_for(first, timeout=2)

            assert (
                await _pending_owner(
                    h.async_redis, h.config.stream, h.config.consumer_group, entry_id
                )
                == h.config.consumer_name
            )
            assert not consumer._inflight_ids
            assert not consumer._inflight
            assert not consumer._peer_absent_since
            assert consumer._sem._value == 1  # noqa: SLF001 - generation accounting invariant
            assert not [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and task.get_name().startswith("consumer:")
            ]

            # This models the top-level supervisor's clean retry generation. It
            # must recover the row canceled under its own stable consumer name;
            # otherwise the prompt path skips that name and the row waits for
            # the 15-minute XAUTOCLAIM fallback.
            hold.set()
            h.runner.hold = None
            # The canceled aiohttp stream can die before FakeRunner's normal
            # epilogue clears this test-only flag. A replacement sandbox starts
            # idle, so reset the fake's process-local state explicitly.
            h.runner.turn_active = False
            h.runner.tail = []
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer._liveness_store = ConsumerLivenessStore(h.async_redis)
            second = asyncio.create_task(consumer.run())
            await _wait_until(
                lambda: h.runner.opened.count("restart") == 2, timeout=3
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                summary = await h.async_redis.xpending(
                    h.config.stream, h.config.consumer_group
                )
                if summary["pending"] == 0:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("restart generation did not ack its own reclaimed entry")
            consumer.request_stop()
            await second

            assert h.runner.opened.count("restart") == 2
            assert await _deliveries(
                h.async_redis, h.config.stream, h.config.consumer_group
            ) == {}
            assert (await h.async_redis.xpending(h.config.stream, h.config.consumer_group))[
                "pending"
            ] == 0

    asyncio.run(go())


def test_prompt_selection_stays_timely_while_heavy_reclaim_waits_for_capacity(
    make_harness,
) -> None:
    async def go() -> None:
        async with make_harness(
            reclaim_min_idle_ms=80,
            dead_consumer_idle_ms=0,
            consumer_heartbeat_ttl_ms=30,
            consumer_capability_ttl_ms=160,
        ) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="held")]
            h.runner.tail = [Final(text="done", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis, kernel=h.kernel, config=h.config, max_concurrency=1
            )
            await consumer.ensure_group()

            # Occupy the only handler slot. Reclaimed handlers will block in
            # _dispatch after ownership transfer; that wait must not retain the
            # shared selection lock.
            await consumer._dispatch(
                "local-only",
                to_stream_fields(_qevent("local", event_id="local-only")),
            )
            await _wait_until(lambda: h.runner.turn_active)

            old_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("old", event_id="old"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "backstop-peer", {h.config.stream: ">"}, count=1
            )
            await asyncio.sleep(0.09)

            prompt_id = await h.async_redis.xadd(
                h.config.stream, to_stream_fields(_qevent("prompt", event_id="prompt"))
            )
            assert await h.async_redis.xreadgroup(
                h.config.consumer_group, "prompt-peer", {h.config.stream: ">"}, count=1
            )
            store = ConsumerLivenessStore(h.async_redis)
            await store.publish(
                stream=h.config.stream,
                group=h.config.consumer_group,
                consumer="prompt-peer",
                heartbeat_ttl_ms=1,
                capability_ttl_ms=h.config.consumer_capability_ttl_ms,
            )
            await _wait_key(
                h.async_redis,
                consumer_heartbeat_key(h.config.stream, h.config.consumer_group, "prompt-peer"),
                present=False,
            )

            heavy = asyncio.create_task(consumer._reclaim_once())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if (
                    await _pending_owner(
                        h.async_redis, h.config.stream, h.config.consumer_group, old_id
                    )
                    == h.config.consumer_name
                ):
                    break
                await asyncio.sleep(0.005)
            assert (
                await _pending_owner(
                    h.async_redis, h.config.stream, h.config.consumer_group, old_id
                )
                == h.config.consumer_name
            )
            assert not heavy.done(), "heavy dispatch should be blocked on the occupied semaphore"

            assert await consumer._prompt_reclaim_once() == 0
            await asyncio.sleep(h.config.consumer_heartbeat_ttl_ms / 1000 + 0.015)
            prompt = asyncio.create_task(consumer._prompt_reclaim_once())
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                if (
                    await _pending_owner(
                        h.async_redis, h.config.stream, h.config.consumer_group, prompt_id
                    )
                    == h.config.consumer_name
                ):
                    break
                await asyncio.sleep(0.005)
            assert (
                await _pending_owner(
                    h.async_redis, h.config.stream, h.config.consumer_group, prompt_id
                )
                == h.config.consumer_name
            )
            assert not prompt.done(), "prompt dispatch should now wait outside the selection lock"

            for task in (heavy, prompt):
                task.cancel()
            await asyncio.gather(heavy, prompt, return_exceptions=True)
            hold.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*list(consumer._inflight), return_exceptions=True),
                    timeout=1,
                )

    asyncio.run(go())


def test_next_turn_drains_queued_eval_reset_before_claiming(make_harness) -> None:
    """#1534: eval-owned sandboxes are SADDed onto THREAD_RESET_SET after each
    case. The next runs-lane turn must release them before it claims, so a
    following eval case or cluster message does not wait for the 30s
    maintenance tick (or the 90s claim timeout) with the quota already full.
    """

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("eval-case-1", thread="tEval1"))
            assert h.substrate.lookup("tEval1") is not None

            await h.async_redis.sadd(THREAD_RESET_SET, "tEval1")
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            nxt = _qevent("eval-case-2", thread="tEval2", event_id="eval-2")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(nxt))
            await consumer._sem.acquire()
            await consumer._handle(entry_id, to_stream_fields(nxt))

            assert h.substrate.lookup("tEval1") is None
            assert h.substrate.lookup("tEval2") is not None
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0
            assert not await h.async_redis.sismember(THREAD_RESET_INFLIGHT_SET, "tEval1")

    asyncio.run(go())


def test_next_turn_drains_reset_before_claiming_when_quota_is_full(make_harness) -> None:
    """#1534: drain must run BEFORE the follow-up claim. If it ran after
    process_event, tEval2 would see ResourceQuota still full (tEval1 still
    holding the slot), raise CapacityExhaustedError, and never bind.
    """

    async def go() -> None:
        async with make_harness(claim_timeout_seconds=0.2) as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("eval-case-1", thread="tEval1"))
            assert h.substrate.lookup("tEval1") is not None

            h.fake_k8s.quota_rejection = QuotaRejection(
                quota_name="curie-sandbox-quota",
                resource="limits.cpu",
                requested="1",
                used="8",
                hard="8",
            )
            original_delete = h.fake_k8s.delete_claim

            def delete_and_free(name: str) -> None:
                original_delete(name)
                h.fake_k8s.quota_rejection = None

            h.fake_k8s.delete_claim = delete_and_free  # type: ignore[method-assign]

            await h.async_redis.sadd(THREAD_RESET_SET, "tEval1")
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            nxt = _qevent("eval-case-2", thread="tEval2", event_id="eval-2-quota")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(nxt))
            await consumer._sem.acquire()
            await consumer._handle(entry_id, to_stream_fields(nxt))

            assert h.substrate.lookup("tEval1") is None
            assert h.substrate.lookup("tEval2") is not None

    asyncio.run(go())


def test_maintenance_tick_drains_pending_thread_reset_requests(make_harness) -> None:
    """#713: an operator-requested thread reset (the API SADDs the thread_key
    into THREAD_RESET_SET) is picked up and applied by the maintenance tick,
    releasing that thread's sandbox and popping it off the pending set."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tDrain"))
            assert h.substrate.lookup("tDrain") is not None

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, "tDrain")

            await consumer._drain_thread_reset_requests()

            assert h.substrate.lookup("tDrain") is None  # released
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0  # popped, not left behind
            # #812: the in-progress marker is cleared only after the release
            # actually lands, so a successful drain leaves nothing pending.
            assert not await h.async_redis.sismember(THREAD_RESET_INFLIGHT_SET, "tDrain")

    asyncio.run(go())


def test_maintenance_tick_thread_reset_failed_release_keeps_the_signal_pending(
    make_harness, caplog
) -> None:
    """#812 (was #806 incomplete): the observable "reset outstanding" signal --
    membership of THREAD_RESET_SET UNION THREAD_RESET_INFLIGHT_SET, which the
    API's ``is_pending`` and therefore the CLI's ``reset-thread`` poll read --
    must NOT flip to done when ``release_thread`` raises or times out. The drain
    SPOPs the request (the atomic claim) and moves it into the in-progress set,
    clearing it only on SUCCESS; a failed release leaves the key in the
    in-progress set, so the signal stays pending and the CLI reports the reset as
    unconfirmed rather than a false ``released: true`` (scenario B)."""

    async def go() -> None:
        async with make_harness() as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, "tFailRelease")

            async def boom_release(thread_key: str) -> bool:
                raise RuntimeError("injected release failure")

            h.kernel.release_thread = boom_release  # type: ignore[method-assign]

            with caplog.at_level(logging.ERROR):
                await consumer._drain_thread_reset_requests()

            # Claimed off the request set (atomic SPOP: no second replica double-releases)...
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0
            # ...but the in-progress marker is STILL set: the pending signal the
            # CLI gates on stays True, so it never reports a false success.
            assert await h.async_redis.sismember(THREAD_RESET_INFLIGHT_SET, "tFailRelease")
            assert any("tFailRelease" in r.getMessage() for r in caplog.records)

    asyncio.run(go())


def test_maintenance_tick_thread_reset_is_not_stalled_by_a_wedged_runner(
    make_harness, monkeypatch
) -> None:
    """#739: the maintenance tick runs stream reclaim, orphan reaping, and the
    thread-reset drain in one pass, so a reset whose runner never answers the
    courtesy interrupt would otherwise block all three for the runner client's
    full 600s request timeout -- and the request is already SPOPped off the set,
    so it is lost rather than retried on the next tick. The drain must therefore
    finish in seconds and the sandbox must actually be gone afterwards."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tWedgedDrain"))
            assert h.substrate.lookup("tWedgedDrain") is not None

            monkeypatch.setattr(kernel_module, "_RESET_INTERRUPT_TIMEOUT_S", 0.2)

            wedged = asyncio.Event()  # never set

            async def never_answers(base_url: str, reason: str, token: str | None = None) -> None:
                await wedged.wait()

            monkeypatch.setattr(h.kernel._runner, "interrupt", never_answers)

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, "tWedgedDrain")

            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=2.0)

            assert h.substrate.lookup("tWedgedDrain") is None  # the reset was not lost

    asyncio.run(go())


def test_maintenance_tick_thread_reset_is_a_noop_when_nothing_pending(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer._drain_thread_reset_requests()  # must not raise

    asyncio.run(go())


def test_maintenance_tick_thread_reset_one_failure_does_not_block_the_rest(
    make_harness, caplog
) -> None:
    """A release failure for one requested thread (e.g. a transient substrate
    error) is logged and does not prevent the rest of the batch from being
    drained -- an operator resetting several stuck threads at once should not
    have one bad apple silently strand the others unprocessed."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tOk"))

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, "tBoom", "tOk")

            original_release_thread = h.kernel.release_thread

            async def flaky_release_thread(thread_key: str) -> bool:
                if thread_key == "tBoom":
                    raise RuntimeError("injected substrate failure")
                return await original_release_thread(thread_key)

            h.kernel.release_thread = flaky_release_thread  # type: ignore[method-assign]

            with caplog.at_level(logging.ERROR):
                await consumer._drain_thread_reset_requests()

            assert h.substrate.lookup("tOk") is None  # still processed despite tBoom's failure
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0  # both popped either way
            assert any("tBoom" in r.getMessage() for r in caplog.records)

    asyncio.run(go())


def test_maintenance_tick_reset_drain_has_a_per_tick_budget_and_defers_the_rest(
    make_harness, monkeypatch
) -> None:
    """#743: a large operator-populated batch of wedged resets must not cost
    N x the per-request release bound inline in one maintenance tick -- that
    re-crosses the same multi-hundred-second stall #739 set out to eliminate,
    just scaled by batch size instead of by the runner's HTTP timeout. The
    drain now stops once its per-tick time budget is spent and leaves
    whatever is left in THREAD_RESET_SET for a later tick, so one call to
    ``_drain_thread_reset_requests`` never blocks proportionally to N."""

    async def go() -> None:
        async with make_harness() as h:
            budget_s = 0.2
            monkeypatch.setattr(consumer_module, "_THREAD_RESET_DRAIN_BUDGET_S", budget_s)

            processed: list[str] = []

            async def slow_release_thread(thread_key: str) -> bool:
                processed.append(thread_key)
                await asyncio.sleep(0.05)  # each request "wedged" for a while
                return True

            h.kernel.release_thread = slow_release_thread  # type: ignore[method-assign]

            # A batch large enough that draining it all at 0.05s/request would
            # take roughly 1s -- five times the budget.
            keys = [f"tBatch{i}" for i in range(20)]
            await h.async_redis.sadd(THREAD_RESET_SET, *keys)

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            start = time.monotonic()
            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=2.0)
            elapsed = time.monotonic() - start

            # Bounded by the budget (plus slack for the one in-flight request
            # that pushed the check past it), not by N * per-request cost.
            assert elapsed < 0.6
            assert len(processed) < len(keys)  # did not drain the whole batch in one pass
            remaining = await h.async_redis.scard(THREAD_RESET_SET)
            assert remaining > 0  # the rest is left for the next tick, not lost

            # A later tick picks up where this one left off: draining again
            # (with the budget restored to a generous value) finishes the batch.
            monkeypatch.setattr(consumer_module, "_THREAD_RESET_DRAIN_BUDGET_S", 30.0)
            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=5.0)
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0
            assert len(processed) == len(keys)

    asyncio.run(go())


def test_maintenance_tick_thread_reset_is_not_stalled_by_a_hanging_substrate_release(
    make_harness, monkeypatch
) -> None:
    """#743: the courtesy interrupt bound (#739) only covers a wedged runner.
    `release_thread`'s own substrate release runs on a bare `asyncio.to_thread`
    with no timeout, so a hang in the K8s control plane -- a claim delete that
    never returns -- would stall the tick just as unboundedly. The release
    call must be bounded the same way the interrupt already is."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tHangRelease"))
            assert h.substrate.lookup("tHangRelease") is not None

            monkeypatch.setattr(kernel_module, "_RESET_RELEASE_TIMEOUT_S", 0.2)

            def hanging_release(thread_key: str) -> bool:
                time.sleep(5.0)  # never returns within the test's window
                return True

            monkeypatch.setattr(h.substrate, "release", hanging_release)

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, "tHangRelease")

            # Must finish well under the 5s hang, bounded instead by the
            # (monkeypatched) release timeout.
            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=2.0)

            # The request was popped either way; a fresh reset is needed to retry.
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0

    asyncio.run(go())
