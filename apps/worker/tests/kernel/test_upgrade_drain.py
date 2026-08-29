"""A routine chart upgrade must not turn accepted work into an escalation (#2010).

The incident this file is written against, in full:

    11:15:23  the worker claimed a real AgentMail turn
    11:15:44  `helm upgrade` (atomic) began; the backing services and the worker
              rolled together
    11:16:31  the REPLACEMENT worker reclaimed the delivery, saw the durable
              side-effect marker, and emitted the designed escalation --
              "A prior attempt started an action before the worker restarted;
              not retrying automatically. Flagging for a human."
    11:16:41  that reply was delivered

Nothing was lost and no side effect ran twice. The requested task simply did not
happen. That escalation is the correct answer to an unsafe situation; the bug is
that a supported, routine upgrade CREATES the situation.

Two tests carry the property, and they are a matched pair:

- ``test_a_rollout_over_an_accepted_side_effecting_turn_escalates_it`` is the
  NEGATIVE CONTROL. It reproduces the incident with the gate bypassed. Without
  it, "the fixed path completed the turn" would be indistinguishable from a
  harness that cannot produce the failure at all.
- ``test_the_pre_upgrade_gate_refuses_the_roll_and_the_turn_completes_once`` is
  the regression. The gate refuses the upgrade while the accepted turn is still
  in flight, nothing is rolled, and the turn reaches its intended terminal
  outcome exactly once.

Reverts that turn this red, each named against the property it breaks:

- Return ``()`` unconditionally from ``UpgradeDrainGate.unsettled_deliveries``
  (or gate on pending-ness rather than lease liveness) -> the gate reports a
  clean drain over a live delivery and the regression's refusal assertion fails.
- Drop the ``_claims_paused`` check from ``StreamConsumer._consume`` or from
  ``Consumer._maintenance_loop``'s reclaim -> the replacement claims during the
  drain and the suppression test fails.
- Clear the quiesce flag inside ``await_drained``'s success path -> the
  replacement pods reclaim mid-roll, which is the incident itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

from aci_protocol import (
    Final,
    QueuedTurn,
    ReplyHandle,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    TurnSource,
)
from curie_dispatcher.queue import to_stream_fields
from curie_worker.consumer import Consumer
from curie_worker.delivery_lease import DeliveryLeaseStore
from curie_worker.upgrade_drain import UpgradeDrainGate

DONE = SessionStatus.DONE

# The escalation the incident produced, verbatim from ``Kernel._process_one``.
# Byte-identical on purpose: these tests assert the FAILURE is reproduced in the
# control and absent after the fix, and a paraphrase would pass both ways.
ESCALATION = (
    "A prior attempt started an action before the worker restarted; "
    "not retrying automatically. Flagging for a human."
)

# Compressed lease clocks (the ``test_delivery_lease.py`` shape) so a rollout
# window is seconds rather than a minute. Every config ratio is preserved:
# TTL >= 3x heartbeat, reclaim interval < TTL, runner ceiling <= budget, and the
# quiesce TTL strictly outlasts the drain wait.
_KNOBS: dict[str, object] = {
    "delivery_budget_s": 60.0,
    "delivery_lease_ttl_s": 1.0,
    "delivery_lease_heartbeat_s": 0.3,
    "runner_total_timeout_s": 30.0,
    "reclaim_interval_s": 0.05,
    "reclaim_min_idle_ms": 50,
    "upgrade_drain_timeout_s": 0.5,
    "upgrade_drain_poll_interval_s": 0.05,
    "upgrade_quiesce_ttl_s": 5.0,
}


def _qevent(text: str, *, thread: str, event_id: str) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(kind="slack", channel="C1", placeholder="p-1"),
        received_at="2026-08-28T11:15:23+00:00",
        source=TurnSource.SLACK,
    )


async def _wait_until(predicate, *, timeout: float = 20.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached within timeout")


async def _stop(task: asyncio.Task[None], consumer: Consumer | None = None) -> None:
    if consumer is not None:
        consumer.request_stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _kill_replica(task: asyncio.Task[None], consumer: Consumer) -> None:
    """Model the Pod going away mid-turn, which is what a roll does.

    Cancelling only ``run()`` is not that: the per-message handlers are separate
    tasks and would keep executing (and keep holding the thread lock), so the
    replacement would block on a replica that in reality no longer exists. The
    handler tasks go too, exactly as a SIGKILLed process's would.
    """
    inflight = list(consumer._inflight)
    await _stop(task, consumer)
    for handler in inflight:
        handler.cancel()
    for handler in inflight:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handler


def _texts(h) -> list[str]:  # noqa: ANN001
    """Every reply body the sink saw, whether edited into a placeholder or posted."""
    return [text for _addr, _ref, text in h.sink.updates] + [
        text for _addr, _ref, text in h.sink.text_posts
    ]


def _admit_side_effecting_turn(h, consumer: Consumer):  # noqa: ANN001, ANN201
    """Script the runner so the turn flags a side effect and then hangs.

    This is the incident's "deliberate real AgentMail turn": the durable
    no-retry marker is written the instant the frame is seen, and the turn is
    still open. Returns the hold event the test releases.
    """
    hold = asyncio.Event()
    h.runner.hold = hold
    h.runner.default_script = [
        SideEffectFlag(tool="agentmail.send", call_id="call-1"),
        TextDelta(text="sending the mail"),
    ]
    h.runner.tail = [Final(text="mail sent", status=DONE)]
    assert consumer is not None
    return hold


# --- the negative control: the incident, reproduced ---------------------------


def test_a_rollout_over_an_accepted_side_effecting_turn_escalates_it(make_harness) -> None:  # noqa: ANN001
    """Reproduce #2010 with the gate bypassed.

    The old replica accepts the turn and flags a side effect; the roll takes it
    away mid-flight; the replacement reclaims, refuses to re-run the action, and
    escalates. Exactly what happened at 11:16:31, and exactly what the gate
    exists to stop happening on a routine upgrade.
    """
    thread = "drain-control-1"
    event_id = uuid.uuid4().hex

    async def go() -> None:
        async with make_harness(**_KNOBS) as h:
            store = DeliveryLeaseStore(h.async_redis, h.config)
            old_cfg = h.config.model_copy(update={"consumer_name": "worker-old"})
            new_cfg = h.config.model_copy(update={"consumer_name": "worker-new"})
            old = Consumer(redis=h.async_redis, kernel=h.kernel, config=old_cfg, leases=store)
            new = Consumer(redis=h.async_redis, kernel=h.kernel, config=new_cfg, leases=store)
            _admit_side_effecting_turn(h, old)

            # The group is created at ``$``, so it must exist before the enqueue.
            await old.ensure_group()
            old_task = asyncio.create_task(old.run())
            new_task: asyncio.Task[None] | None = None
            try:
                await h.async_redis.xadd(
                    h.config.stream,
                    to_stream_fields(_qevent("send the mail", thread=thread, event_id=event_id)),
                )
                # Admitted: the turn is open and the durable marker is written.
                await _wait_until(lambda: h.runner.turn_active)
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if await h.async_redis.exists(h.config.side_effect_key(event_id)):
                        break
                    await asyncio.sleep(0.02)
                assert await h.async_redis.exists(h.config.side_effect_key(event_id)), (
                    "the side-effect marker was never written; nothing was admitted"
                )

                # The roll: the old worker goes away mid-turn, exactly as a Pod
                # being replaced does. NO gate runs.
                await _kill_replica(old_task, old)

                new_task = asyncio.create_task(new.run())
                await _wait_until(lambda: any(ESCALATION in t for t in _texts(h)), timeout=30.0)

                # The incident's shape, asserted: escalated, delivered, and the
                # requested task never happened.
                assert any(ESCALATION in t for t in _texts(h))
                assert not any("mail sent" in t for t in _texts(h)), (
                    "the control did not actually reproduce the failure"
                )
            finally:
                if new_task is not None:
                    await _stop(new_task, new)
                if not old_task.done():
                    await _stop(old_task, old)

    asyncio.run(go())


# --- the regression: refuse the roll, finish the work -------------------------


def test_the_pre_upgrade_gate_refuses_the_roll_and_the_turn_completes_once(make_harness) -> None:  # noqa: ANN001
    """The same accepted turn, with the pre-upgrade gate in front of the roll.

    The gate finds a live-leased delivery, refuses, and `helm upgrade` fails
    with it -- so nothing is rolled, the turn keeps running on the workers that
    are already there, and it reaches its intended terminal outcome exactly
    once. The no-duplicate-side-effect invariant is asserted on the far side: a
    redelivery of the same event after the turn is done runs no second action.
    """
    thread = "drain-fixed-1"
    event_id = uuid.uuid4().hex

    async def go() -> None:
        async with make_harness(**_KNOBS) as h:
            store = DeliveryLeaseStore(h.async_redis, h.config)
            gate = UpgradeDrainGate(h.async_redis, h.config)
            old_cfg = h.config.model_copy(update={"consumer_name": "worker-old"})
            old = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=old_cfg,
                leases=store,
                drain=gate,
            )
            hold = _admit_side_effecting_turn(h, old)

            await old.ensure_group()
            old_task = asyncio.create_task(old.run())
            try:
                await h.async_redis.xadd(
                    h.config.stream,
                    to_stream_fields(_qevent("send the mail", thread=thread, event_id=event_id)),
                )
                await _wait_until(lambda: h.runner.turn_active)
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if await h.async_redis.exists(h.config.side_effect_key(event_id)):
                        break
                    await asyncio.sleep(0.02)
                assert await h.async_redis.exists(h.config.side_effect_key(event_id))

                # The chart's pre-upgrade hook, before a single manifest is
                # applied. It must refuse: an accepted, side-effecting delivery
                # is live.
                outcome = await gate.await_drained()
                assert outcome.drained is False, (
                    "the gate cleared an upgrade over a live side-effecting delivery"
                )
                assert outcome.remaining, "a refusal must name what held it back"
                assert all(
                    r.startswith(f"{h.config.stream}/{h.config.consumer_group}/")
                    for r in outcome.remaining
                )

                # `helm upgrade` fails on that exit code, so NOTHING rolls. The
                # postponed upgrade puts the fleet back the way it found it.
                await gate.clear_quiesce()

                # The turn finishes on the workers that were already there.
                hold.set()
                await _wait_until(lambda: any("mail sent" in t for t in _texts(h)), timeout=30.0)
                await _wait_until(lambda: len(h.sink.completions) >= 1, timeout=30.0)

                assert not any(ESCALATION in t for t in _texts(h)), (
                    "accepted work was still converted into a human escalation"
                )
                assert len(h.runner.opened) == 1, (
                    f"the turn ran more than once: {h.runner.opened!r}"
                )
                assert len(h.sink.completions) == 1, (
                    f"expected exactly one terminal completion, saw {h.sink.completions!r}"
                )

                # The invariant this fix may not trade away: a redelivery of the
                # same event after it is terminally done executes no second
                # action. The kernel's ``is_terminal`` short-circuit is what
                # holds it, and it must still hold with the gate in the path.
                await h.async_redis.xadd(
                    h.config.stream,
                    to_stream_fields(_qevent("send the mail", thread=thread, event_id=event_id)),
                )
                await asyncio.sleep(1.0)
                assert len(h.runner.opened) == 1, (
                    "a redelivery re-ran the side-effecting turn"
                )
            finally:
                hold.set()
                await _stop(old_task, old)

    asyncio.run(go())


# --- new claims are suppressed for the whole drain ----------------------------


def test_a_quiesced_consumer_claims_nothing_and_resumes_when_released(make_harness) -> None:  # noqa: ANN001
    """"...while preventing new claims during the drain."

    A wait that kept admitting work could never terminate under load, and a
    replacement Pod that comes up mid-roll must not reclaim the delivery a
    still-draining replica is settling. Both are the same suppression, so both
    are asserted here: nothing is claimed while the flag is set, and the very
    same entry is claimed once it clears.
    """
    thread = "drain-quiesce-1"
    event_id = uuid.uuid4().hex

    async def go() -> None:
        async with make_harness(**_KNOBS) as h:
            store = DeliveryLeaseStore(h.async_redis, h.config)
            gate = UpgradeDrainGate(h.async_redis, h.config)
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                leases=store,
                drain=gate,
            )
            h.runner.default_script = [Final(text="handled", status=DONE)]

            await consumer.ensure_group()
            await gate.request_quiesce()
            task = asyncio.create_task(consumer.run())
            try:
                await h.async_redis.xadd(
                    h.config.stream,
                    to_stream_fields(_qevent("do the thing", thread=thread, event_id=event_id)),
                )
                # Long enough for several read-loop cycles (read_block_ms is
                # 100ms in the harness) and several maintenance ticks.
                await asyncio.sleep(1.5)
                assert h.runner.opened == [], (
                    "a quiesced consumer claimed new work during the drain"
                )

                await gate.clear_quiesce()
                await _wait_until(lambda: len(h.runner.opened) == 1, timeout=30.0)
                assert h.runner.opened == ["do the thing"]
            finally:
                await _stop(task, consumer)

    asyncio.run(go())


def test_a_quiesced_consumer_does_not_reclaim_a_departed_replicas_entry(make_harness) -> None:  # noqa: ANN001
    """The incident's own mechanism, blocked.

    A reclaim IS a new claim: it moves a peer's pending entry into this consumer
    and dispatches it. That is precisely how the replacement Pod took the
    delivery at 11:16:31 and escalated it. The read-loop suppression above
    cannot cover this -- a reclaimed entry never goes through ``XREADGROUP`` --
    so it is asserted separately, against an entry left pending by a replica
    that is already gone (no live lease, idle past the reclaim threshold).

    Red on removing the ``_claims_paused`` guard in ``Consumer._maintenance_loop``.
    """
    thread = "drain-reclaim-1"
    event_id = uuid.uuid4().hex

    async def go() -> None:
        async with make_harness(**_KNOBS) as h:
            store = DeliveryLeaseStore(h.async_redis, h.config)
            gate = UpgradeDrainGate(h.async_redis, h.config)
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                leases=store,
                drain=gate,
            )
            h.runner.default_script = [Final(text="handled", status=DONE)]

            await consumer.ensure_group()
            await h.async_redis.xadd(
                h.config.stream,
                to_stream_fields(_qevent("finish the thing", thread=thread, event_id=event_id)),
            )
            # Read it into a consumer name that no longer exists: the departed
            # replica's PEL row, holding no lease.
            await h.async_redis.xreadgroup(
                h.config.consumer_group,
                "worker-departed",
                {h.config.stream: ">"},
                count=1,
            )
            await asyncio.sleep(h.config.reclaim_min_idle_ms / 1000 + 0.1)

            await gate.request_quiesce()
            task = asyncio.create_task(consumer.run())
            try:
                await asyncio.sleep(1.5)
                assert h.runner.opened == [], (
                    "a quiesced consumer reclaimed a departed replica's delivery"
                )

                await gate.clear_quiesce()
                await _wait_until(lambda: len(h.runner.opened) == 1, timeout=30.0)
                assert h.runner.opened == ["finish the thing"]
            finally:
                await _stop(task, consumer)

    asyncio.run(go())
