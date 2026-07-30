"""An in-memory reference runner producing a canonical outbound stream.

This is not a real harness. It is the reference implementation the conformance
suite runs against until D1 wires the suite to the real claude-agent-sdk runner.
Given an inbound message it returns the NDJSON lines a well behaved runner would
emit for that message, so the conformance checks have something concrete and
protocol-correct to validate.
"""

from collections.abc import Iterable

from .events import (
    Event,
    Final,
    Interrupt,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from .ndjson import to_ndjson_line


def reference_producer(message: Event | Interrupt) -> Iterable[str]:
    """Return the NDJSON lines a compliant runner would emit for ``message``."""

    if isinstance(message, Interrupt):
        return [
            to_ndjson_line(Final(text="run interrupted", status=SessionStatus.IDLE_AWAITING_INPUT))
        ]

    if message.type == "eval_case":
        return [
            to_ndjson_line(TextDelta(text="evaluating")),
            to_ndjson_line(Final(text="eval complete", status=SessionStatus.DONE)),
        ]

    return [
        to_ndjson_line(TextDelta(text="Looking into it")),
        to_ndjson_line(ToolNote(text="running the search tool", tool="search")),
        to_ndjson_line(SideEffectFlag(tool="send_email", detail="delivered 1 message")),
        to_ndjson_line(Final(text="all done", status=SessionStatus.DONE)),
    ]
