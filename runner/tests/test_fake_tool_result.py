"""The fake model returns tool results, so the offline path can carry them.

The fake is the mock at the adapter seam: everything above it runs unmodified.
That is only true for the parts of the SDK's message stream it actually
produces, and it produced no tool result at all -- so
``translate.py::_translate_user``, the whole closing half of a side-effecting
call, was unreachable in every offline run. It stopped being a cosmetic gap when
the ACI began carrying a call's result (ADR-0117): the field an undo acts on
arrives only on a tool result, and nothing offline could produce one.

Two shapes, because a connector's reply has two honest outcomes and they take
different paths through the platform: prose (nothing to restore) and a structured
reply that reports what it read and what it left.
"""

from __future__ import annotations

import json

from aci_protocol import SideEffectFlag
from claude_agent_sdk import ToolResultBlock, UserMessage
from curie_runner import SideEffectClassifier
from curie_runner.fake import (
    REVERSIBLE_MARKER,
    FakeModelSession,
    default_turn,
    reversible_turn,
)
from curie_runner.translate import TurnState, translate_message


def _tool_results(script: list[object]) -> list[ToolResultBlock]:
    return [
        block
        for message in script
        if isinstance(message, UserMessage) and not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


def _frames(script: list[object]) -> list[SideEffectFlag]:
    state = TurnState()
    frames: list[SideEffectFlag] = []
    for message in script:
        for event in translate_message(message, state, SideEffectClassifier(), None):
            if isinstance(event, SideEffectFlag):
                frames.append(event)
    return frames


def test_the_default_turn_answers_its_own_tool_call() -> None:
    """A call with no result is a turn that died mid-call, not a normal one."""

    results = _tool_results(default_turn())

    assert [r.tool_use_id for r in results] == ["t1"]
    assert results[0].is_error is False


def test_the_default_turn_closes_its_side_effecting_call() -> None:
    """Two frames, joined -- the shape a ledger records as one completed action."""

    frames = _frames(default_turn())

    assert len(frames) == 2
    assert {f.call_id for f in frames} == {"t1"}
    assert frames[0].arguments == {"command": "echo hi"}
    # Bash answering in prose is the not-undoable path, and it is the honest
    # default: `echo hi` has no prior state to report.
    assert frames[1].result is None
    assert frames[1].failed is False


def test_the_reversible_turn_reports_what_it_read_and_what_it_left() -> None:
    """The undoable path, offline: the reply carries prior, post and target."""

    frames = _frames(reversible_turn())

    assert len(frames) == 2
    closing = frames[1]
    assert closing.result is not None
    assert closing.result["prior"] == {"spec": {"replicas": 3}}
    assert closing.result["post"] == {"spec": {"replicas": 10}}
    assert closing.result["target"]["name"] == "api"


def test_the_marker_selects_the_reversible_turn() -> None:
    """Selected the way the approval marker is, so one fake serves both paths."""

    session = FakeModelSession()

    async def _drive() -> list[object]:
        await session.query(f"scale it {REVERSIBLE_MARKER}")
        return [message async for message in session.receive_turn()]

    import asyncio

    script = asyncio.run(_drive())
    results = _tool_results(script)
    assert results, "the reversible turn must answer its own call"
    assert json.loads(str(results[0].content))["ok"] is True


def test_an_unmarked_query_still_gets_the_default_turn() -> None:
    session = FakeModelSession()

    async def _drive() -> list[object]:
        await session.query("just do it")
        return [message async for message in session.receive_turn()]

    import asyncio

    script = asyncio.run(_drive())
    assert str(_tool_results(script)[0].content).strip() == "hi"
