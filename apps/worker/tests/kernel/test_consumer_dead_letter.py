"""Dead-letter tests: a permanently-failing entry is bounded by a delivery cap
instead of being reclaimed and re-dispatched forever (#505).

Against the real Valkey stream + consumer group, like ``test_consumer.py``.

Valkey delivery-count semantics, pinned empirically against the live Valkey
(localhost:26379) before these tests were written -- the Implementer MUST build
to the same understanding:

  * ``XADD`` alone puts nothing in the PEL; ``times_delivered`` does not exist
    until an entry is delivered to a consumer.
  * The FIRST ``XREADGROUP ... >`` delivery creates the PEL entry with
    ``times_delivered == 1``. So the counter is a count of deliveries ALREADY
    MADE, not of retries remaining.
  * Every ``XAUTOCLAIM`` (and ``XREADGROUP ... 0`` pending-replay) INCREMENTS
    ``times_delivered`` by exactly 1, and ``XPENDING`` reports the POST-claim
    value. Observed: XREADGROUP -> 1, XAUTOCLAIM -> 2, -> 3, -> 4.

The consequence for the cap: reading ``times_delivered`` via ``XPENDING``
*before* the claim yields the number of deliveries already made, so the cap
check is ``times_delivered >= max_delivery`` -> dead-letter without claiming.
Reading it *after* an ``XAUTOCLAIM`` would already include the current claim's
bump and needs ``>``. These tests are written against the XPENDING-first shape
the plan recommends, but they assert only on the OBSERVABLE contract -- the
number of times the handler is invoked -- so either implementation shape passes
as long as the boundary is right.

The contract these tests pin: ``max_delivery`` is the maximum number of times an
entry may be DELIVERED to a handler. Once an entry has been delivered
``max_delivery`` times and still failed, the next reclaim dead-letters it
instead of re-dispatching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import curie_worker.consumer as consumer_module
import pytest
from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus
from curie_dispatcher.queue import to_stream_fields
from curie_worker.config import WorkerConfig
from curie_worker.consumer import Consumer
from curie_worker.dead_letter_alert import install_dead_letter_alerting
from pydantic import ValidationError

DONE = SessionStatus.DONE


def _qevent(text: str, *, thread: str, event_id: str) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id,
        conversation_id=thread,
        author="U1",
        text=text,
        # The #505 shape: a reply endpoint that is durably persisted but dead.
        reply_handle=ReplyHandle(
            kind="slack", channel="C1", placeholder="p-1", endpoint="http://localhost:8155/api/"
        ),
        received_at="2026-07-05T00:00:00+00:00",
    )


def _dead_stream(config: WorkerConfig) -> str:
    """The graveyard name, asked of the config rather than re-derived here.

    Re-deriving ``<stream>:dead`` in the test would mirror the implementation, so
    a change to the derivation would move both sides together and the suite would
    never notice. Calling the same helper the consumer calls means these tests
    fail loudly if the name changes.
    """
    return config.dead_letter_stream_name()


async def _deliver_new(consumer: Consumer, h) -> int:
    """Take initial delivery as THIS consumer and dispatch, exactly as the read
    loop does -- so every delivery corresponds to one handler invocation.

    Driving the read loop by hand (rather than ``consumer.run()``) keeps the
    reclaim count deterministic: ``run()``'s maintenance loop would reclaim on
    its own timer and race the assertions.
    """
    delivered = await h.async_redis.xreadgroup(
        h.config.consumer_group,
        h.config.consumer_name,
        {h.config.stream: ">"},
        count=10,
    )
    n = 0
    for _stream, entries in delivered or []:
        for entry_id, fields in entries:
            await consumer._dispatch(entry_id, fields)
            n += 1
    await asyncio.gather(*list(consumer._inflight))
    return n


async def _reclaim_and_settle(consumer: Consumer) -> None:
    await consumer._reclaim_once()
    await asyncio.gather(*list(consumer._inflight))


async def _pending_rows(h) -> dict[str, int]:
    """Every pending entry id -> its current ``times_delivered``."""
    rows = await h.async_redis.xpending_range(
        h.config.stream,
        h.config.consumer_group,
        min="-",
        max="+",
        count=50,
    )
    return {row["message_id"]: int(row["times_delivered"]) for row in rows}


async def _pending_ids(h) -> set[str]:
    return set(await _pending_rows(h))


async def _dead_rows(h) -> list[tuple[str, dict[str, str]]]:
    return await h.async_redis.xrange(_dead_stream(h.config))


def test_permanently_failing_entry_is_dead_lettered_at_the_cap_and_group_progresses(
    make_harness,
) -> None:
    """A poison entry is dead-lettered after exactly ``max_delivery`` handler
    invocations, and a healthy entry alongside it still completes (the
    head-of-line proof: the poison never stalls the group).
    """

    async def go() -> None:
        async with make_harness(max_delivery=3, reclaim_min_idle_ms=0) as h:
            h.runner.default_script = [Final(text="answer", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            # The controllable-failure seam: process_event raises forever for the
            # poison event (the #505 dead-endpoint shape -- the reply POST to the
            # long-gone CLI stub raises out of the kernel), and delegates to the
            # real kernel for the healthy one.
            real_process = h.kernel.process_event
            calls: dict[str, int] = {"poison": 0, "healthy": 0}

            async def counting(qevent: QueuedTurn) -> None:
                calls[qevent.event_id] += 1
                if qevent.event_id == "poison":
                    raise RuntimeError("simulated dead reply endpoint")
                await real_process(qevent)

            h.kernel.process_event = counting  # type: ignore[method-assign,assignment]

            poison = _qevent("poison turn", thread="tdl-poison", event_id="poison")
            healthy = _qevent("healthy turn", thread="tdl-healthy", event_id="healthy")
            poison_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(poison))
            await h.async_redis.xadd(h.config.stream, to_stream_fields(healthy))

            try:
                # Delivery #1 for both: the healthy entry succeeds and acks; the
                # poison entry raises and is left pending.
                assert await _deliver_new(consumer, h) == 2

                # Reclaim until the poison entry leaves the main group's PEL --
                # bounded well above the cap so a missing cap fails the test by
                # assertion, not by hanging forever.
                for _ in range(12):
                    if poison_id not in await _pending_ids(h):
                        break
                    await _reclaim_and_settle(consumer)

                # AC-1: the poison entry is on the graveyard, with the original
                # payload preserved verbatim plus namespaced failure metadata.
                rows = await _dead_rows(h)
                assert len(rows) == 1, f"expected exactly one dead-letter row, got {rows}"
                _dl_id, dl = rows[0]
                assert dl["dl_original_id"] == poison_id
                assert dl["dl_delivery_count"] == "3"
                assert dl["dl_reason"]
                assert dl["dl_dead_lettered_at"]
                # The original entry fields survive so a human can inspect/replay.
                # The frozen #7 wire encoding is a single JSON blob under
                # "payload" (curie_dispatcher.queue.to_stream_fields), not
                # flat top-level fields, so decode it before asserting.
                payload = json.loads(dl["payload"])
                assert payload["event_id"] == "poison"
                assert payload["text"] == "poison turn"

                # AC-1: and it is gone from the main group's PEL, so it is never
                # reclaimed again.
                assert poison_id not in await _pending_ids(h)

                # AC-5 / the exact boundary: the handler ran exactly max_delivery
                # times -- not one fewer (dying early would break crash recovery),
                # not one more (an off-by-one in the cap).
                assert calls["poison"] == 3

                # AC-2, the head-of-line proof and the issue's core severity: the
                # healthy entry enqueued alongside the poison one was processed to
                # completion, and the group's PEL is now empty -- forward progress,
                # no stall.
                assert h.runner.opened == ["healthy turn"]
                assert h.sink.last_text == "answer"
                assert calls["healthy"] == 1
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_transient_failure_reclaims_and_acks_without_dead_lettering(
    make_harness,
) -> None:
    """The cap must not break ADR-0013 crash recovery: a failure that stops
    failing is retried by reclaim and eventually acked, never dead-lettered.

    Deliberately set at the boundary (``max_delivery=3``, succeeding on delivery
    #3) so this pins the cap from the opposite side to test 1: the LAST permitted
    delivery must still happen. An implementation that reads the delivery count
    AFTER ``XAUTOCLAIM``'s own bump and still compares ``>=`` would kill the entry
    one delivery early and fail here, while test 1 (which catches a cap that runs
    one delivery too long) would still pass. Together they nail the off-by-one.
    """

    async def go() -> None:
        async with make_harness(max_delivery=3, reclaim_min_idle_ms=0) as h:
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            # Fails the first two deliveries (a worker crash / transient blip),
            # then the real kernel handles it on delivery #3 -- the last delivery
            # max_delivery=3 permits.
            real_process = h.kernel.process_event
            calls = {"n": 0}

            async def flaky(qevent: QueuedTurn) -> None:
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError("simulated transient failure")
                await real_process(qevent)

            h.kernel.process_event = flaky  # type: ignore[method-assign,assignment]

            qe = _qevent("orphan", thread="tdl-transient", event_id="transient")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            try:
                assert await _deliver_new(consumer, h) == 1  # delivery #1: fails

                for _ in range(8):
                    if entry_id not in await _pending_ids(h):
                        break
                    await _reclaim_and_settle(consumer)

                # It got there on the third delivery, by reclaim -- exactly the
                # crash-recovery behavior the module exists for.
                assert calls["n"] == 3
                assert h.runner.opened == ["orphan"]
                assert h.sink.last_text == "recovered"

                # Acked off the main stream...
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
                # ...and NOT dead-lettered. A cap that fired here would have
                # thrown away a turn that was going to succeed.
                assert await _dead_rows(h) == []
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_unparseable_entry_is_dead_lettered_not_silently_dropped(
    make_harness,
) -> None:
    """An entry the frozen queue contract cannot parse goes to the graveyard so
    it is observable, instead of being silently acked into the void.
    """

    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            token = uuid.uuid4().hex[:8]
            entry_id = await h.async_redis.xadd(
                h.config.stream, {"garbage": "x", "trace": token}
            )

            try:
                assert await _deliver_new(consumer, h) == 1

                rows = await _dead_rows(h)
                assert len(rows) == 1, f"unparseable entry never reached the graveyard: {rows}"
                _dl_id, dl = rows[0]
                assert dl["dl_original_id"] == entry_id
                assert "unparseable" in dl["dl_reason"]
                assert dl["dl_dead_lettered_at"]
                # The raw fields are kept verbatim -- the whole point is that a
                # human can see WHAT failed to parse.
                assert dl["garbage"] == "x"
                assert dl["trace"] == token

                # Acked off the main group: a poison entry must not be reclaimed
                # forever either.
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
                # And the kernel never saw it.
                assert h.runner.opened == []
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_cap_binds_beyond_the_first_pending_page(
    make_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every reclaim candidate is cap-checked, not just the first XPENDING page.

    ``XAUTOCLAIM`` pages through the ENTIRE pending list via its cursor, so a
    cap check that inspects only one ``COUNT _CAP_SCAN_PAGE`` page lets every
    entry past the head of the list be claimed and re-dispatched over its budget
    -- the bounded-delivery guarantee silently not holding at backlog scale.

    The scan's page size is a module constant (1000), not a config knob, so it is
    pinned to 1 here: three poison entries then span three pages and the paging
    is actually exercised. Without this the backlog would fit in one page and the
    test would pass against a single-page scan -- i.e. prove nothing.
    """
    monkeypatch.setattr(consumer_module, "_CAP_SCAN_PAGE", 1)

    async def go() -> None:
        async with make_harness(max_delivery=2, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            calls: dict[str, int] = {"p0": 0, "p1": 0, "p2": 0}

            async def always_fails(qevent: QueuedTurn) -> None:
                calls[qevent.event_id] += 1
                raise RuntimeError("simulated dead reply endpoint")

            h.kernel.process_event = always_fails  # type: ignore[method-assign,assignment]

            ids: dict[str, str] = {}
            for name in ("p0", "p1", "p2"):
                qe = _qevent(f"{name} turn", thread=f"tdl-page-{name}", event_id=name)
                ids[name] = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            try:
                # Delivery #1 for all three; every one fails and stays pending.
                assert await _deliver_new(consumer, h) == 3
                # Reclaim #1: all are at 1 < max_delivery=2, so all are claimed
                # and re-dispatched -- delivery #2, the last one permitted.
                await _reclaim_and_settle(consumer)
                assert calls == {"p0": 2, "p1": 2, "p2": 2}

                # Reclaim #2: all three are now over cap. Every one must be
                # dead-lettered; none may be dispatched a third time. Against the
                # single-page scan only p0 (the first page) is cap-checked, while
                # XAUTOCLAIM's own paging still claims and dispatches p1 and p2.
                await _reclaim_and_settle(consumer)

                assert calls == {"p0": 2, "p1": 2, "p2": 2}, (
                    "an entry beyond the first pending page was dispatched over the cap"
                )
                rows = await _dead_rows(h)
                dead_originals = {dl["dl_original_id"] for _dl_id, dl in rows}
                assert dead_originals == set(ids.values()), (
                    f"not every over-cap entry reached the graveyard: {rows}"
                )
                for _dl_id, dl in rows:
                    assert dl["dl_delivery_count"] == "2"
                # ...and the whole group's PEL is drained: no stall left behind.
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_a_failing_dead_letter_does_not_kill_the_rest_of_the_tick(
    make_harness,
    caplog: Any,
) -> None:
    """A graveyard XADD that fails for ONE entry is isolated.

    ``_dead_letter_over_cap`` is the first, unguarded await of the maintenance
    tick. If a single entry's XADD raises (an unwritable dead stream, a
    WRONGTYPE key), an unisolated failure propagates out of ``_reclaim_once`` --
    so XAUTOCLAIM never runs and ``reap_orphans`` never runs, on EVERY tick.
    That is #505's own stall class: one bad entry silently killing crash
    recovery for the whole group.
    """

    async def go() -> None:
        async with make_harness(max_delivery=2, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            calls: dict[str, int] = {"bad": 0, "good": 0}

            async def always_fails(qevent: QueuedTurn) -> None:
                calls[qevent.event_id] += 1
                raise RuntimeError("simulated dead reply endpoint")

            h.kernel.process_event = always_fails  # type: ignore[method-assign,assignment]

            reaped = {"n": 0}
            real_reap = h.kernel.reap_orphans

            async def counting_reap() -> None:
                reaped["n"] += 1
                await real_reap()

            h.kernel.reap_orphans = counting_reap  # type: ignore[method-assign,assignment]

            # "bad" is XADDed first, so it is the first over-cap id the scan
            # reaches: an unisolated raise there never gets to "good".
            bad_id = await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("bad turn", thread="tdl-iso-bad", event_id="bad")),
            )
            good_id = await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("good turn", thread="tdl-iso-good", event_id="good")),
            )

            # The graveyard is unwritable for exactly one entry.
            dead = _dead_stream(h.config)
            real_xadd = h.async_redis.xadd

            async def failing_xadd(name: str, fields: dict[str, str], **kw: Any) -> Any:
                if name == dead and fields.get("dl_original_id") == bad_id:
                    raise RuntimeError("graveyard unwritable")
                return await real_xadd(name, fields, **kw)

            consumer._redis.xadd = failing_xadd  # type: ignore[method-assign,assignment]

            try:
                assert await _deliver_new(consumer, h) == 2  # delivery #1: both fail
                await _reclaim_and_settle(consumer)  # delivery #2: both fail
                assert calls == {"bad": 2, "good": 2}

                # The tick, exactly as _maintenance_loop runs it. Pre-fix, the
                # raise from bad's XADD escapes _reclaim_once and reap never runs.
                with caplog.at_level(logging.ERROR):
                    await _reclaim_and_settle(consumer)
                    await h.kernel.reap_orphans()

                # The failure is loud, and names the entry and the cause.
                assert any(
                    bad_id in r.getMessage() and r.exc_info for r in caplog.records
                ), "the failed dead-letter was not logged with the entry id"

                # The OTHER entry was still cap-checked and dead-lettered.
                rows = await _dead_rows(h)
                assert [dl["dl_original_id"] for _dl_id, dl in rows] == [good_id]

                # XAUTOCLAIM still ran on that same tick: it claimed the still-
                # pending bad entry, bumping its PEL delivery count to 3...
                assert (await _pending_rows(h)).get(bad_id) == 3
                # ...but the cap still binds -- it was never dispatched again.
                assert calls == {"bad": 2, "good": 2}
                # ...and reap_orphans still ran.
                assert reaped["n"] == 1
            finally:
                consumer._redis.xadd = real_xadd  # type: ignore[method-assign]
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_graveyard_is_bounded_by_dead_letter_maxlen(
    make_harness,
) -> None:
    """The graveyard XADD is capped, so poison at ingest rate cannot OOM Valkey.

    The unparseable path dead-letters per INBOUND entry, so a wire-format drift
    that makes entries unparseable en masse grows the graveyard as fast as the
    dispatcher produces -- on the same Valkey that holds the kernel's per-thread
    locks and side-effect markers. This drives a flood well past a tiny
    ``dead_letter_maxlen`` and asserts the graveyard LENGTH actually stays
    bounded; an implementation that merely reads the knob and still XADDs
    unbounded ends at ``flood`` rows and fails.

    ``approximate=True`` lets Valkey trim on whole-node boundaries, so the bound
    is "at least maxlen", not exactly it -- hence a generous ceiling rather than
    an exact count (observed: 44 rows for this flood). The signal is
    bounded-vs-linear growth, not the precise trim point.
    """

    flood = 400
    # Comfortably above the observed trimmed length yet far below the unbounded
    # one, so the assertion distinguishes the two without pinning Valkey's
    # internal node size.
    ceiling = 200

    async def go() -> None:
        async with make_harness(dead_letter_maxlen=1, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            for i in range(flood):
                # Unparseable: the per-inbound-entry dead-letter path.
                await h.async_redis.xadd(h.config.stream, {"garbage": str(i)})

            try:
                # _deliver_new reads a bounded page, so drain to empty.
                total = 0
                while (n := await _deliver_new(consumer, h)) > 0:
                    total += n
                assert total == flood

                dead_len = await h.async_redis.xlen(_dead_stream(h.config))
                assert dead_len < flood, (
                    f"graveyard grew unbounded: {dead_len} rows from {flood} "
                    "dead-letters -- the maxlen bound is not applied"
                )
                assert dead_len <= ceiling, (
                    f"graveyard length {dead_len} exceeds the expected bound for "
                    f"dead_letter_maxlen=1 after {flood} dead-letters"
                )

                # The bound must not cost the ack: every entry is still off the
                # group, even the ones whose graveyard row was evicted.
                summary = await h.async_redis.xpending(
                    h.config.stream, h.config.consumer_group
                )
                assert summary["pending"] == 0
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_unparseable_entry_records_its_real_reclaimed_delivery_count(
    make_harness,
) -> None:
    """An unparseable entry reclaimed after a crash records its ACTUAL count.

    An entry can be delivered, have its worker die before it ever parses, and be
    reclaimed -- so by the time it is dead-lettered the PEL says 2+, not 1. A
    hardcoded 1 fabricates ``dl_delivery_count`` precisely during crash recovery,
    which is when the graveyard's operational evidence matters most. Here the
    first delivery is taken WITHOUT dispatching (the crashed worker), so the
    reclaim that follows is delivery #2.
    """

    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            entry_id = await h.async_redis.xadd(h.config.stream, {"garbage": "x"})

            try:
                # Delivery #1, taken and then "crashed": claimed into the PEL but
                # never dispatched, so it was never parsed and never acked.
                await h.async_redis.xreadgroup(
                    h.config.consumer_group,
                    h.config.consumer_name,
                    {h.config.stream: ">"},
                    count=10,
                )
                assert (await _pending_rows(h))[entry_id] == 1

                # Delivery #2, by reclaim: now it parses, fails, and dead-letters.
                await _reclaim_and_settle(consumer)

                rows = await _dead_rows(h)
                assert len(rows) == 1, f"expected one dead-letter row, got {rows}"
                _dl_id, dl = rows[0]
                assert dl["dl_original_id"] == entry_id
                assert "unparseable" in dl["dl_reason"]
                # The real reclaimed count, not a fabricated 1.
                assert dl["dl_delivery_count"] == "2", (
                    "the graveyard fabricated the delivery count of a reclaimed "
                    "unparseable entry instead of reading the PEL"
                )
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_unparseable_entry_cannot_clobber_or_forge_its_own_dl_metadata(
    make_harness,
) -> None:
    """The graveyard's ``dl_*`` metadata is the consumer's, never the payload's.

    The unparseable path stores an ARBITRARY malformed field map, so the ``dl_``
    prefix is a convention the payload is under no obligation to respect. An
    entry carrying its own ``dl_original_id`` must not be able to overwrite the
    real one (forging the record) nor have its own value silently destroyed (the
    graveyard then no longer preserves the original verbatim). The metadata wins;
    the original survives under a doubled prefix.
    """

    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            entry_id = await h.async_redis.xadd(
                h.config.stream,
                {
                    "dl_original_id": "forged-id",
                    "dl_reason": "forged-reason",
                    "dl_delivery_count": "999",
                    "dl_dead_lettered_at": "forged-time",
                    "garbage": "x",
                },
            )

            try:
                assert await _deliver_new(consumer, h) == 1

                rows = await _dead_rows(h)
                assert len(rows) == 1, f"expected one dead-letter row, got {rows}"
                _dl_id, dl = rows[0]

                # The consumer's metadata wins outright -- the payload cannot forge it.
                assert dl["dl_original_id"] == entry_id
                assert "unparseable" in dl["dl_reason"]
                assert dl["dl_delivery_count"] == "1"
                assert dl["dl_dead_lettered_at"] != "forged-time"

                # ...and the original is still preserved verbatim, recoverable by
                # stripping exactly one "dl_" from the escaped key.
                assert dl["dl_dl_original_id"] == "forged-id"
                assert dl["dl_dl_reason"] == "forged-reason"
                assert dl["dl_dl_delivery_count"] == "999"
                assert dl["dl_dl_dead_lettered_at"] == "forged-time"
                # Non-colliding fields are untouched.
                assert dl["garbage"] == "x"
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_over_cap_entry_whose_message_was_trimmed_is_dead_lettered_and_acked(
    make_harness,
) -> None:
    """An id can sit in the PEL while its message is gone from the stream.

    A trim (MAXLEN) or an XDEL removes the message but NOT the pending entry, so
    the over-cap path's XRANGE comes back empty. It must still write a
    metadata-only graveyard row and -- the load-bearing half -- still XACK. Skip
    the XACK and the id stays pending forever: exactly the #505 stall the cap
    exists to end, now permanent because it can never be dispatched again either.

    ``_dead_letter_over_cap`` is driven directly rather than via
    ``_reclaim_once`` so the assertion sees the XACK and nothing else: a later
    XAUTOCLAIM purges PEL ids whose messages were deleted, which would mask a
    missing XACK entirely.
    """

    async def go() -> None:
        async with make_harness(max_delivery=2, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            calls = {"n": 0}

            async def always_fails(qevent: QueuedTurn) -> None:
                calls["n"] += 1
                raise RuntimeError("simulated dead reply endpoint")

            h.kernel.process_event = always_fails  # type: ignore[method-assign,assignment]

            qe = _qevent("trimmed turn", thread="tdl-trimmed", event_id="trimmed")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            try:
                assert await _deliver_new(consumer, h) == 1  # delivery #1: fails
                await _reclaim_and_settle(consumer)  # delivery #2: fails, at cap
                assert calls["n"] == 2

                # The message is trimmed off the stream while its id stays pending.
                assert await h.async_redis.xdel(h.config.stream, entry_id) == 1
                assert await h.async_redis.xrange(h.config.stream, min=entry_id, max=entry_id) == []
                assert entry_id in await _pending_ids(h)

                over_cap = await consumer._dead_letter_over_cap()
                assert over_cap == {entry_id}

                # A metadata-only row: everything the operator can still know.
                rows = await _dead_rows(h)
                assert len(rows) == 1, (
                    f"the trimmed over-cap entry never reached the graveyard: {rows}"
                )
                _dl_id, dl = rows[0]
                assert dl["dl_original_id"] == entry_id
                assert dl["dl_delivery_count"] == "2"
                assert dl["dl_reason"]
                assert dl["dl_dead_lettered_at"]
                # Metadata-ONLY: there is no original payload left to preserve.
                assert set(dl) == {
                    "dl_original_id",
                    "dl_delivery_count",
                    "dl_reason",
                    "dl_dead_lettered_at",
                }

                # The XACK happened: the PEL is drained, so the stall is gone.
                assert entry_id not in await _pending_ids(h)
                summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
                assert summary["pending"] == 0
                # ...and it was never dispatched again on the way out.
                assert calls["n"] == 2
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_dead_letter_is_logged_loudly_with_the_operational_facts(
    make_harness,
    caplog: Any,
) -> None:
    """Dead-lettering is silent data loss unless it is loud.

    The row itself is best-effort (the graveyard is MAXLEN-bounded and has no
    consumer group), so the ERROR log is the only durable trace an operator is
    guaranteed to see. It must carry the entry id, the delivery count, the
    reason, and where the row went.
    """

    async def go() -> None:
        async with make_harness(max_delivery=2, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            async def always_fails(qevent: QueuedTurn) -> None:
                raise RuntimeError("simulated dead reply endpoint")

            h.kernel.process_event = always_fails  # type: ignore[method-assign,assignment]

            qe = _qevent("poison turn", thread="tdl-log", event_id="poison")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))

            try:
                assert await _deliver_new(consumer, h) == 1  # delivery #1: fails
                await _reclaim_and_settle(consumer)  # delivery #2: fails, at cap

                with caplog.at_level(logging.ERROR):
                    await _reclaim_and_settle(consumer)  # dead-letters

                dead = _dead_stream(h.config)
                # ONE record must carry every fact -- an operator reading a single
                # line has to be able to act on it, not correlate across lines.
                # (The per-delivery "left pending" ERRORs are also captured here;
                # none of them is this record.)
                messages = [
                    r.getMessage() for r in caplog.records if r.levelno == logging.ERROR
                ]
                assert any(
                    entry_id in m
                    and "2" in m
                    and "max-delivery-exceeded" in m
                    and dead in m
                    for m in messages
                ), (
                    "no single ERROR log carried the entry id, delivery count, "
                    f"reason and target stream; got: {messages}"
                )
            finally:
                await h.async_redis.delete(_dead_stream(h.config))

    asyncio.run(go())


def test_dead_letter_emits_one_retention_independent_critical_alert(
    make_harness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_logger = logging.getLogger("curie_worker.consumer")
    original_handlers = list(source_logger.handlers)
    original_propagate = source_logger.propagate
    for handler in original_handlers:
        source_logger.removeHandler(handler)
    source_logger.propagate = False

    async def go() -> None:
        async with make_harness(max_delivery=2, reclaim_min_idle_ms=0) as h:
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            async def always_fails(qevent: QueuedTurn) -> None:
                raise RuntimeError("simulated dead reply endpoint")

            h.kernel.process_event = always_fails  # type: ignore[method-assign,assignment]

            qe = _qevent("poison turn", thread="tdl-alert", event_id="poison-alert")
            entry_id = await h.async_redis.xadd(h.config.stream, to_stream_fields(qe))
            dead = _dead_stream(h.config)

            try:
                assert await _deliver_new(consumer, h) == 1
                await _reclaim_and_settle(consumer)
                assert entry_id in await _pending_ids(h)
                await _reclaim_and_settle(consumer)

                assert await h.async_redis.delete(dead) == 1

                alerts = [
                    record
                    for record in caplog.records
                    if record.name == "curie_worker.alerts.dead_letter"
                    and record.levelno == logging.CRITICAL
                ]
                assert len(alerts) == 1, f"expected one dead letter alert, got {alerts}"
                assert alerts[0].getMessage() == (
                    f"event=curie.dead_letter entry_id={entry_id} delivery_count=2 "
                    f"reason=max-delivery-exceeded dead_stream={dead}"
                )

                caplog.clear()
                source_logger.error("unrelated consumer error for entry %s", entry_id)
                source_logger.error(
                    "dead-lettered entry %s after %d deliveries reason=%s -> %s",
                    entry_id,
                    2,
                    "max-delivery-exceeded",
                    dead,
                )
                source_logger.error(
                    "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
                    entry_id,
                    2,
                    "max-delivery-exceeded",
                )
                assert not [
                    record
                    for record in caplog.records
                    if record.name == "curie_worker.alerts.dead_letter"
                    and record.levelno == logging.CRITICAL
                ]

                caplog.clear()
                child_logger = logging.getLogger("curie_worker.consumer.child")
                child_logger.error(
                    "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
                    entry_id,
                    2,
                    "max-delivery-exceeded",
                    dead,
                )
                assert not [
                    record
                    for record in caplog.records
                    if record.name == "curie_worker.alerts.dead_letter"
                    and record.levelno == logging.CRITICAL
                ]

                caplog.clear()
                source_logger.critical(
                    "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
                    entry_id,
                    2,
                    "max-delivery-exceeded",
                    dead,
                )
                assert not [
                    record
                    for record in caplog.records
                    if record.name == "curie_worker.alerts.dead_letter"
                    and record.levelno == logging.CRITICAL
                ]

                caplog.clear()
                source_logger.error(
                    "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
                    entry_id,
                    "two",
                    "max-delivery-exceeded",
                    dead,
                )
                assert not [
                    record
                    for record in caplog.records
                    if record.name == "curie_worker.alerts.dead_letter"
                    and record.levelno == logging.CRITICAL
                ]
            finally:
                await h.async_redis.delete(dead)

    caplog.clear()
    try:
        install_dead_letter_alerting()
        install_dead_letter_alerting()
        with caplog.at_level(logging.ERROR):
            asyncio.run(go())
    finally:
        for handler in list(source_logger.handlers):
            source_logger.removeHandler(handler)
        for handler in original_handlers:
            source_logger.addHandler(handler)
        source_logger.propagate = original_propagate


def test_max_delivery_below_the_floor_is_rejected() -> None:
    """``max_delivery=1`` dead-letters every ordinary worker crash on its first
    reclaim -- ADR-0013 crash recovery relies on a reclaim actually retrying.

    The ``ge=2`` floor is the only thing standing between a config typo and a
    consumer that throws away every recoverable turn, so pin it: a future edit
    dropping the constraint fails here instead of shipping.
    """
    with pytest.raises(ValidationError):
        WorkerConfig(stream="curie:runs", max_delivery=1)
    with pytest.raises(ValidationError):
        WorkerConfig(stream="curie:runs", max_delivery=0)

    # The floor itself, and the default above it, stay valid.
    assert WorkerConfig(stream="curie:runs", max_delivery=2).max_delivery == 2
    assert WorkerConfig(stream="curie:runs").max_delivery >= 2


def test_dead_letter_stream_equal_to_source_stream_is_rejected() -> None:
    """A graveyard pointed at its own source stream is a config error, not a
    runtime surprise.

    ``_dead_letter`` XADDs the payload to the target and only then XACKs it, so
    target == source re-queues every failure onto the stream it came from: a
    valid failure is re-consumed under a fresh id, and an unparseable one spins a
    hot loop -- the exact permanent stall the delivery cap exists to prevent.
    Rejecting at construction means an operator learns at boot, not mid-incident.
    """
    with pytest.raises(ValidationError, match="must not equal CURIE_STREAM"):
        WorkerConfig(stream="curie:runs", dead_letter_stream="curie:runs")

    # The derived default can never collide, so it stays valid...
    assert WorkerConfig(stream="curie:runs").dead_letter_stream == ""
    # ...and so does any genuinely distinct override.
    assert (
        WorkerConfig(
            stream="curie:runs", dead_letter_stream="curie:runs:dead"
        ).dead_letter_stream
        == "curie:runs:dead"
    )
