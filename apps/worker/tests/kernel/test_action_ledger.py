"""The kernel records what a turn did to the world (ADR-0117).

The branch under test is the one that already existed: a ``side_effect_flag``
sets ``saw_side_effect`` and persists the no-retry marker. It now also writes a
record, and the constraints on that are as much about what must NOT change.

`kernel.py` is sacred under ADR-0013, so every test here that is not about the
ledger is about the signal the ledger must not disturb.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, SideEffectFlag, TurnSource
from curie_worker.actions import ActionBackendError, RecordedAction

DONE = SessionStatus.DONE


def _qevent(text: str, *, thread: str = "th-1", event_id: str | None = None) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(kind="slack", channel="C1", placeholder="p-1"),
        received_at="2026-07-05T00:00:00+00:00",
        source=TurnSource.SLACK,
    )


@dataclass
class FakeRecorder:
    """Records the calls the kernel makes, and can refuse one."""

    fail_on_record: bool = False
    recorded: list[dict[str, Any]] = field(default_factory=list)
    completed: list[tuple[str, SideEffectFlag]] = field(default_factory=list)

    async def record(
        self,
        frame: SideEffectFlag,
        *,
        event_id: str,
        conversation_id: str,
        agent_id: str | None,
        gate_approval_id: str | None = None,
    ) -> RecordedAction:
        if self.fail_on_record:
            raise ActionBackendError("ledger down")
        self.recorded.append(
            {
                "frame": frame,
                "event_id": event_id,
                "conversation_id": conversation_id,
                "agent_id": agent_id,
                "gate_approval_id": gate_approval_id,
            }
        )
        return RecordedAction(id=f"a{len(self.recorded)}", status="pending")

    async def complete(self, action_id: str, frame: SideEffectFlag) -> None:
        self.completed.append((action_id, frame))


def _call(call_id: str, tool: str = "scale_deployment") -> list[SideEffectFlag]:
    """The two frames one side-effecting call produces."""

    return [
        SideEffectFlag(
            tool=tool,
            call_id=call_id,
            arguments={"replicas": 10},
            detail="non-idempotent tool executed",
        ),
        SideEffectFlag(
            tool=tool,
            call_id=call_id,
            failed=False,
            result={"ok": True, "prior": {"spec": {"replicas": 3}}},
            detail="non-idempotent tool completed",
        ),
    ]


def test_each_call_becomes_one_record(make_harness) -> None:
    """Two calls, two records. The turn is not the unit; the action is."""

    async def go() -> None:
        recorder = FakeRecorder()
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [
                *_call("toolu_01"),
                *_call("toolu_02", tool="restart_deployment"),
                Final(text="done", status=DONE),
            ]
            await h.kernel.process_event(_qevent("scale it"))

            assert [r["frame"].call_id for r in recorder.recorded] == ["toolu_01", "toolu_02"]
            assert [action_id for action_id, _ in recorder.completed] == ["a1", "a2"]

    asyncio.run(go())


def test_a_record_carries_the_turn_it_belongs_to(make_harness) -> None:
    async def go() -> None:
        recorder = FakeRecorder()
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            event = _qevent("scale it", thread="th-9")
            await h.kernel.process_event(event)

            assert recorder.recorded[0]["conversation_id"] == "th-9"
            assert recorder.recorded[0]["event_id"] == event.event_id

    asyncio.run(go())


def test_the_completion_carries_what_the_connector_reported(make_harness) -> None:
    async def go() -> None:
        recorder = FakeRecorder()
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("scale it"))

            _, frame = recorder.completed[0]
            assert frame.result == {"ok": True, "prior": {"spec": {"replicas": 3}}}

    asyncio.run(go())


def test_a_frame_without_a_call_id_records_nothing_and_still_blocks_retry(
    make_harness,
) -> None:
    """A producer that predates ADR-0117 emits the old frame, and must still work.

    ADR-0036's reader policy cuts both ways: the platform tolerates the older
    producer. There is nothing to record without a call id -- two such frames
    cannot be told apart -- but the no-retry rule reads presence, and presence is
    exactly what this frame still carries.
    """

    async def go() -> None:
        recorder = FakeRecorder()
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [
                SideEffectFlag(tool="deploy"),
                Final(text="done", status=DONE),
            ]
            event = _qevent("deploy")
            await h.kernel.process_event(event)

            assert recorder.recorded == []
            assert await h.async_redis.exists(h.config.side_effect_key(event.event_id))

    asyncio.run(go())


def test_an_unwired_ledger_does_not_break_a_turn(make_harness) -> None:
    """Every existing test builds a kernel with no recorder, and must keep passing."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("scale it"))

            assert h.sink.last_text == "done"

    asyncio.run(go())


def test_a_ledger_that_refuses_the_write_fails_the_turn(make_harness) -> None:
    """A change to the world the platform has no record of is not a success.

    This same branch already fails the turn when the no-retry marker cannot be
    persisted. Losing the record of WHAT changed is not the lesser failure, and a
    turn that reports success while the ledger silently missed an action is how
    an operator learns to distrust the receipt.
    """

    async def go() -> None:
        recorder = FakeRecorder(fail_on_record=True)
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            event = _qevent("scale it")
            await h.kernel.process_event(event)

            # Escalated, not completed: the turn does not report the work done
            # when the platform cannot say what it did.
            assert h.sink.last_text is not None
            assert "human" in h.sink.last_text.lower()
            assert "done" not in h.sink.last_text
            # The side effect still happened, so no retry may follow it, and
            # exactly one attempt was made.
            assert await h.async_redis.exists(h.config.side_effect_key(event.event_id))
            assert h.runner.opened == ["scale it"]

    asyncio.run(go())


def test_a_call_that_ran_under_an_approval_records_which_one(make_harness) -> None:
    """ADR-0117 decision 3 needs to know what authorized the forward call.

    A gated tool only ever executes on the resume turn an approval created, and
    that turn's event id is the approval's own deterministic key. So the gate is
    already in the kernel's hand -- it is the same string ``_is_approval_resume``
    reads for card teardown -- and recording it costs a lookup of nothing.
    """

    async def go() -> None:
        recorder = FakeRecorder()
        approval_id = "3f1b9c22-0000-4000-8000-000000000001"
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            await h.kernel.process_event(
                _qevent("scale it", event_id=f"approval-{approval_id}-resolved")
            )

            assert recorder.recorded[0]["gate_approval_id"] == approval_id

    asyncio.run(go())


def test_an_ordinary_turn_records_no_gate(make_harness) -> None:
    """NULL means ungated, so an ordinary turn must not invent one."""

    async def go() -> None:
        recorder = FakeRecorder()
        async with make_harness(actions=recorder) as h:
            h.runner.default_script = [*_call("toolu_01"), Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("scale it"))

            assert recorder.recorded[0]["gate_approval_id"] is None

    asyncio.run(go())
