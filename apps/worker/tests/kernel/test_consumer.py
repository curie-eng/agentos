"""Consumer tests: end-to-end stream consumption and crash-recovery reclaim,
against the real Valkey stream + consumer group.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

import redis.exceptions
from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from curie_dispatcher.queue import to_stream_fields
from curie_worker import consumer as consumer_module
from curie_worker import kernel as kernel_module
from curie_worker.behaviorpacks import BehaviorPacks
from curie_worker.consumer import (
    THREAD_RESET_INFLIGHT_SET,
    THREAD_RESET_SET,
    Consumer,
)
from curie_worker.sandbox import QuotaRejection
from curie_worker.workspace import WorkspacePreparationError

DONE = SessionStatus.DONE


def _qevent(text: str, *, thread: str = "th-1", event_id: str | None = None) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(kind="slack", channel="C1", placeholder="p-1"),
        received_at="2026-07-05T00:00:00+00:00",
    )


def _thread_key(thread: str) -> str:
    return f"slack:C1:{thread}"


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


def test_message_settlement_does_not_sample_queue_inventory(make_harness) -> None:
    """Queue gauge reads cannot retain the per-message semaphore slot."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="answer", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            observations = 0

            async def observe() -> None:
                nonlocal observations
                observations += 1

            consumer._observe_queue_state = observe  # type: ignore[method-assign]
            await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("hot path", thread="t-hot", event_id="hot-1")),
            )
            rows = await h.async_redis.xreadgroup(
                h.config.consumer_group,
                h.config.consumer_name,
                {h.config.stream: ">"},
                count=1,
            )
            entry_id, fields = rows[0][1][0]
            await consumer._dispatch(entry_id, fields)
            await asyncio.gather(*list(consumer._inflight))

            assert observations == 0
            # Every configured slot is immediately acquirable again. If the
            # handler were still awaiting queue observation in its finally, the
            # last acquire would time out.
            for _ in range(16):
                await asyncio.wait_for(consumer._sem.acquire(), timeout=0.1)
            for _ in range(16):
                consumer._sem.release()

    asyncio.run(go())


def test_maintenance_tick_samples_queue_inventory_once(make_harness) -> None:
    """The maintenance cadence owns queue inventory observation."""

    async def go() -> None:
        async with make_harness() as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            calls: list[str] = []

            async def step(name: str) -> None:
                calls.append(name)

            async def observe() -> None:
                calls.append("observe")
                consumer.request_stop()

            consumer._reclaim_once = lambda: step("reclaim")  # type: ignore[method-assign]
            h.kernel.reap_orphans = lambda: step("reap")  # type: ignore[method-assign]
            h.kernel.sweep_pending_completions = lambda: step("sweep")  # type: ignore[method-assign]
            consumer._drain_thread_reset_requests = lambda: step("reset")  # type: ignore[method-assign]
            consumer._observe_queue_state = observe  # type: ignore[method-assign]

            await consumer._maintenance_loop()

            assert calls == ["reclaim", "reap", "sweep", "reset", "observe"]

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
        async with make_harness(reclaim_min_idle_ms=900000, dead_consumer_idle_ms=0) as h:
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("orphan", thread="tr-dead", event_id="r-dead")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            dead = await h.async_redis.xreadgroup(
                h.config.consumer_group, "dead-consumer", {h.config.stream: ">"}, count=1
            )
            assert dead
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 1

            reclaimed = await consumer._reclaim_once()
            assert reclaimed == 1
            await _wait_until(lambda: h.sink.last_text == "recovered")
            await asyncio.gather(*list(consumer._inflight))
            assert h.runner.opened == ["orphan"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0

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
        async with make_harness(reclaim_min_idle_ms=900000, dead_consumer_idle_ms=15000) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("live", thread="tr-live", event_id="r-live")
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            live = await h.async_redis.xreadgroup(
                h.config.consumer_group, "live-peer", {h.config.stream: ">"}, count=1
            )
            assert live
            reclaimed = await consumer._reclaim_once()
            assert reclaimed == 0
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 1
            assert h.runner.opened == []

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
            first_thread_key = _thread_key("tEval1")
            second_thread_key = _thread_key("tEval2")
            assert h.substrate.lookup(first_thread_key) is not None

            await h.async_redis.sadd(THREAD_RESET_SET, first_thread_key)
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            nxt = _qevent("eval-case-2", thread="tEval2", event_id="eval-2")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(nxt))
            await consumer._sem.acquire()
            await consumer._handle(entry_id, to_stream_fields(nxt))

            assert h.substrate.lookup(first_thread_key) is None
            assert h.substrate.lookup(second_thread_key) is not None
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0
            assert not await h.async_redis.sismember(
                THREAD_RESET_INFLIGHT_SET, first_thread_key
            )

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
            first_thread_key = _thread_key("tEval1")
            second_thread_key = _thread_key("tEval2")
            assert h.substrate.lookup(first_thread_key) is not None

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

            await h.async_redis.sadd(THREAD_RESET_SET, first_thread_key)
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()
            nxt = _qevent("eval-case-2", thread="tEval2", event_id="eval-2-quota")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(nxt))
            await consumer._sem.acquire()
            await consumer._handle(entry_id, to_stream_fields(nxt))

            assert h.substrate.lookup(first_thread_key) is None
            assert h.substrate.lookup(second_thread_key) is not None

    asyncio.run(go())


def test_maintenance_tick_drains_pending_thread_reset_requests(make_harness) -> None:
    """#713: an operator-requested thread reset (the API SADDs the thread_key
    into THREAD_RESET_SET) is picked up and applied by the maintenance tick,
    releasing that thread's sandbox and popping it off the pending set."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tDrain"))
            thread_key = _thread_key("tDrain")
            assert h.substrate.lookup(thread_key) is not None

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, thread_key)

            await consumer._drain_thread_reset_requests()

            assert h.substrate.lookup(thread_key) is None  # released
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0  # popped, not left behind
            # #812: the in-progress marker is cleared only after the release
            # actually lands, so a successful drain leaves nothing pending.
            assert not await h.async_redis.sismember(THREAD_RESET_INFLIGHT_SET, thread_key)

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
            thread_key = _thread_key("tWedgedDrain")
            assert h.substrate.lookup(thread_key) is not None

            monkeypatch.setattr(kernel_module, "_RESET_INTERRUPT_TIMEOUT_S", 0.2)

            wedged = asyncio.Event()  # never set

            async def never_answers(base_url: str, reason: str, token: str | None = None) -> None:
                await wedged.wait()

            monkeypatch.setattr(h.kernel._runner, "interrupt", never_answers)

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, thread_key)

            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=2.0)

            assert h.substrate.lookup(thread_key) is None  # the reset was not lost

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
            thread_key = _thread_key("tHangRelease")
            assert h.substrate.lookup(thread_key) is not None

            monkeypatch.setattr(kernel_module, "_RESET_RELEASE_TIMEOUT_S", 0.2)

            def hanging_release(thread_key: str) -> bool:
                time.sleep(5.0)  # never returns within the test's window
                return True

            monkeypatch.setattr(h.substrate, "release", hanging_release)

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await h.async_redis.sadd(THREAD_RESET_SET, thread_key)

            # Must finish well under the 5s hang, bounded instead by the
            # (monkeypatched) release timeout.
            await asyncio.wait_for(consumer._drain_thread_reset_requests(), timeout=2.0)

            # The request was popped either way; a fresh reset is needed to retry.
            assert await h.async_redis.scard(THREAD_RESET_SET) == 0

    asyncio.run(go())


# --- An acked entry must never be a silent one (#2004) -----------------------
# The kernel-level regressions call ``process_event`` directly, so they can only
# see the silence. The ack is a CONSUMER fact -- a normal return from
# ``process_event`` is what makes ``Consumer`` XACK -- so the pairing the ticket
# actually reports is only observable here, against the real stream and group.


def _workspace_binding(deployment_id: uuid.UUID) -> object:
    """A workspace-enabled binding carrying a FIXED deployment id.

    Fixed on purpose: the id is what an operator greps the worker log for, so
    the assertion below checks the real value rather than that some uuid landed.
    Defined locally rather than imported from ``test_kernel``; importlib mode
    makes cross-test-module imports fragile.
    """

    class WorkspaceResolved:
        def __init__(self) -> None:
            self.agent_id = uuid.uuid4()
            self.agent_name = "test-agent"
            self.endpoint: str | None = None
            self.adapter: str | None = None
            self.deployment_id = deployment_id
            self.workspace_enabled = True

    class WorkspaceBinding:
        async def resolve(self, _kind: str, _channel: str) -> WorkspaceResolved:
            return WorkspaceResolved()

        def boot_env(
            self,
            _resolved: object,
            _thread_key: str,
            *,
            kind: str | None = None,
            address: str | None = None,
        ) -> dict[str, str]:
            return {}

        def packs_for(self, _resolved: object) -> BehaviorPacks:
            return BehaviorPacks()

    return WorkspaceBinding()


def test_failed_workspace_preparation_acks_the_entry_and_is_not_silent(
    make_harness, caplog
) -> None:
    """#2004 end to end: the reported evidence was a group at ``pending 0`` with
    ``last-delivered-id`` equal to the turn's stream id -- yet no sandbox and an
    empty worker log.

    The refusal half of that symptom is already covered on ``next`` by the INFO
    line the refusal branch emits; this pins the PREPARATION half, which is the
    one still silent here. Selection succeeds and the clone fails, so the turn
    retries to its escalation and the entry is acked -- and only the ack makes
    the silence undiagnosable. An entry left pending is at least visible in
    XPENDING; an acked entry with nothing in the log is indistinguishable from a
    bot that was never mentioned. Driven through the real ``Consumer`` and the
    real stream so the ack is the production one, not an assertion about
    ``process_event``'s return value.
    """

    caplog.set_level(logging.WARNING, logger="curie_worker.kernel")
    deployment_id = uuid.uuid4()

    async def go() -> None:
        async with make_harness(binding=_workspace_binding(deployment_id)) as h:
            # Selection SUCCEEDS -- this is a fault, not a policy refusal -- and
            # the workspace then fails to materialize, which is the path that
            # still ends the turn without naming anything.
            class WorkspaceProbe:
                def select_repository(
                    self,
                    *,
                    thread_key: str,
                    deployment_id: uuid.UUID,
                    author: str,
                    repo_full_name: str | None,
                ) -> str:
                    assert repo_full_name is not None
                    return repo_full_name

                def claim_or_resume_with_handle(self, **kwargs: object) -> object:
                    raise WorkspacePreparationError(
                        "clone", "git clone exited 128: repository not found"
                    )

                def touch(self, thread_key: str, *, ttl_seconds: int) -> bool:
                    return True

                def enumerate_expired(self) -> list[object]:
                    # The consumer's maintenance loop polls this every tick;
                    # without it the loop logs a repeated AttributeError
                    # traceback that pollutes this test's caplog capture.
                    return []

            h.kernel._workspace = WorkspaceProbe()  # type: ignore[assignment]

            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent(
                "Fix https://github.com/acme-corp/acme-bot",
                thread="tAckSilent",
                event_id="ws-ack-1",
            )
            await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            task = asyncio.create_task(consumer.run())
            # Wait for the escalation itself, not its classification string --
            # the turn exhausts its retries and escalates on both the pre-fix
            # and post-fix source, and only the classification name
            # ("runner-error" vs "workspace-error") differs between them.
            # Gating on the new name would time out pre-fix and never reach
            # the assertions below, which is the thing this test exists to
            # pin.
            await _wait_until(
                lambda: h.sink.last_text is not None
                and "Flagging for a human" in h.sink.last_text
            )
            consumer.request_stop()
            await task

            # Half one of the ticket's evidence: the entry really was consumed
            # and acked off the group -- nothing is left for an operator to find.
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0
            assert h.fake_k8s.claim_envs == []  # ...and no sandbox was ever created
            assert h.runner.opened == []  # ...and the runner never saw a turn

            # Half two, asserted second so the pairing is explicit: an acked,
            # sandbox-less turn is only a bug because the worker said nothing
            # about it. This is the assertion that fails on the current base.
            failures = [
                r
                for r in caplog.records
                if r.name == "curie_worker.kernel" and "workspace start failed" in r.getMessage()
            ]
            assert failures, (
                "the entry was acked with no sandbox and no line naming the agent -- "
                f"exactly what #2004 reports: {caplog.text!r}"
            )
            assert all(r.levelno == logging.WARNING for r in failures)
            message = failures[-1].getMessage()
            assert "agent=test-agent" in message, message
            assert f"deployment={deployment_id}" in message, message
            assert "stage=clone" in message, message

            # Last: the escalation is correctly classified post-fix. Placed
            # after the silence assertion above so a pre-fix failure reports
            # the meaningful thing (no warning line) rather than this one.
            assert h.sink.last_text is not None
            assert "workspace-error" in h.sink.last_text

    asyncio.run(go())
