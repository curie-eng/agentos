"""The neutral reply wire: four events, opaque ids, a strict discriminated union.

T-B1 (ADR-0096 phase 2, plan section 7 / Stream B). The wire shape asserted here
is EB-B1's model list: ``ReplyTarget`` is ``{kind, address, conversation_id,
reply_ref}``, ``reply_ref`` is opaque and adapter-minted (D2), ``TurnCompleted``
carries ``event_id`` (revision 4) because per-``event_id`` idempotency is a wire
contract (EB-B6(e)) and is unimplementable without it, and ``NavAffordance`` is
the wire-owned form of the worker-local ``NavPack`` (finding 16) so navigation is
not silently dropped at the seam.

Sibling of ``test_models.py``: same package, same strictness (``extra="forbid"``),
same explicit-version construction (``OutboundMessage(version="1.0", ...)``).
"""

from __future__ import annotations

import json

import pytest
from channel_protocol import OutboundMessage
from channel_protocol.reply import (
    REPLY_WIRE_VERSION,
    NavAffordance,
    ReplyAck,
    ReplyEvent,
    ReplyPost,
    ReplyTarget,
    ReplyUpdate,
    SettledOutcome,
    TurnCompleted,
    TurnStatus,
)
from channel_protocol.schema_export import render_schema
from pydantic import TypeAdapter, ValidationError

_EVENTS = TypeAdapter(ReplyEvent)


def _target(**overrides: object) -> ReplyTarget:
    base: dict[str, object] = {
        "kind": "email",
        "address": "agent@example.test",
        "conversation_id": "thread-9",
        "reply_ref": "msg_abc123",
    }
    base.update(overrides)
    return ReplyTarget(**base)  # type: ignore[arg-type]


def _round_trip(event: ReplyEvent) -> ReplyEvent:
    """Serialize and re-validate THROUGH the discriminated union.

    Validating the concrete class back would not exercise the discriminator,
    which is the half an adapter actually depends on: it receives JSON and must
    resolve which event it holds from the ``event`` field alone.
    """
    return _EVENTS.validate_json(event.model_dump_json())


def test_turn_status_round_trips() -> None:
    event = TurnStatus(
        version=REPLY_WIRE_VERSION,
        event="turn.status",
        target=_target(),
        status="Working on it",
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, TurnStatus)
    assert decoded == event
    assert decoded.target.address == "agent@example.test"


def test_empty_status_is_the_clear_and_survives_the_wire() -> None:
    # EB-B6(a): the terminal ``finally`` keeps only the shimmer clear, expressed
    # as ``TurnStatus(status="")``. An empty string must therefore be a VALID
    # status and must not be coerced to None or dropped by the serializer, or
    # the caption is stranded on every turn.
    event = TurnStatus(
        version=REPLY_WIRE_VERSION, event="turn.status", target=_target(), status=""
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, TurnStatus)
    assert decoded.status == ""
    assert json.loads(event.model_dump_json())["status"] == ""


def test_reply_update_round_trips_with_text() -> None:
    event = ReplyUpdate(
        version=REPLY_WIRE_VERSION,
        event="reply.update",
        target=_target(),
        text="the streamed answer",
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, ReplyUpdate)
    assert decoded.text == "the streamed answer"
    # The three optional halves are genuinely optional: a streamed reply carries
    # text alone, a settled card carries message+settled, and nav rides either.
    assert decoded.message is None
    assert decoded.settled is None
    assert decoded.nav is None


def test_reply_update_round_trips_with_message_and_settled_outcome() -> None:
    # The neutral form of today's ``SettledCard`` (slack_sink.py:41-54): who
    # asked, what was decided, by whom, with what note. ``decision is None`` is
    # the expiry case (#419); a decision is the resolved case (#1084).
    event = ReplyUpdate(
        version=REPLY_WIRE_VERSION,
        event="reply.update",
        target=_target(),
        message=OutboundMessage(version="1.0", text="Deploy this change?"),
        settled=SettledOutcome(
            requested_by="U9", decision="approved", resolver="U7", note="ship it"
        ),
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, ReplyUpdate)
    assert decoded.message is not None
    assert decoded.message.text == "Deploy this change?"
    assert decoded.settled is not None
    assert decoded.settled.decision == "approved"
    assert decoded.settled.resolver == "U7"


def test_settled_outcome_expiry_form_has_no_decision() -> None:
    outcome = SettledOutcome(requested_by="U9")
    assert outcome.decision is None
    assert outcome.resolver is None


def test_nav_affordance_round_trips() -> None:
    # Finding 16: ``NavPack`` is worker-local (behaviorpacks.py:132-141) and
    # cannot be imported here without inverting the package dependency, so the
    # wire owns ``NavAffordance`` and the worker's sink layer maps into it.
    event = ReplyUpdate(
        version=REPLY_WIRE_VERSION,
        event="reply.update",
        target=_target(),
        text="the streamed answer",
        nav=NavAffordance(label="Home", command="hub"),
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, ReplyUpdate)
    assert decoded.nav == NavAffordance(label="Home", command="hub")


def test_reply_post_round_trips() -> None:
    event = ReplyPost(
        version=REPLY_WIRE_VERSION,
        event="reply.post",
        target=_target(),
        message=OutboundMessage(version="1.0", text="Deploy this change?"),
        requested_by="U9",
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, ReplyPost)
    assert decoded.requested_by == "U9"
    assert decoded.message.text == "Deploy this change?"


def test_turn_completed_round_trips_and_carries_the_event_id() -> None:
    # Revision 4 / EB-B6(e): delivery is at-least-once, so the dedupe key has to
    # BE on the event. Without ``event_id`` an adapter cannot tell a duplicate
    # completion from a second turn in the same conversation, which is exactly
    # the guard today's adapter gets wrong (adapter.py:209-214 keys on the
    # conversation).
    event = TurnCompleted(
        version=REPLY_WIRE_VERSION,
        event="turn.completed",
        target=_target(),
        event_id="chn-7f3-a1b2c3d4e5f60718",
        outcome="delivered",
    )
    decoded = _round_trip(event)
    assert isinstance(decoded, TurnCompleted)
    assert decoded.event_id == "chn-7f3-a1b2c3d4e5f60718"
    assert decoded.outcome == "delivered"


def test_turn_completed_requires_an_event_id() -> None:
    with pytest.raises(ValidationError):
        TurnCompleted(  # type: ignore[call-arg]
            version=REPLY_WIRE_VERSION,
            event="turn.completed",
            target=_target(),
            outcome="delivered",
        )


@pytest.mark.parametrize(
    "outcome", ["delivered", "dropped", "escalated", "awaiting-approval"]
)
def test_every_terminal_outcome_is_on_the_wire(outcome: str) -> None:
    # EB-B1's four outcomes, one per terminal path the kernel actually reaches:
    # a finished turn, a polite drop (_drop_with_message), an escalation
    # (_escalate), and a suspension (_pause_for_approval). A missing member here
    # means a terminal path has no honest value to report.
    event = TurnCompleted(
        version=REPLY_WIRE_VERSION,
        event="turn.completed",
        target=_target(),
        event_id="ev-1",
        outcome=outcome,  # type: ignore[arg-type]
    )
    assert _round_trip(event) == event


def test_an_unmodelled_outcome_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TurnCompleted(
            version=REPLY_WIRE_VERSION,
            event="turn.completed",
            target=_target(),
            event_id="ev-1",
            outcome="finished-ish",  # type: ignore[arg-type]
        )


def test_the_union_rejects_an_unknown_event_name() -> None:
    payload = json.dumps(
        {
            "version": REPLY_WIRE_VERSION,
            "event": "reply.delete",
            "target": _target().model_dump(),
        }
    )
    with pytest.raises(ValidationError):
        _EVENTS.validate_json(payload)


@pytest.mark.parametrize(
    "event_name", ["turn.status", "reply.update", "reply.post", "turn.completed"]
)
def test_the_union_dispatches_on_the_event_discriminator(event_name: str) -> None:
    # The discriminator is what lets an adapter switch on one field instead of
    # trying each model in turn, and it is the reason ``event`` is a Literal on
    # every member rather than a free string.
    bodies: dict[str, dict[str, object]] = {
        "turn.status": {"status": "working"},
        "reply.update": {"text": "hi"},
        "reply.post": {
            "message": OutboundMessage(version="1.0", text="hi").model_dump(),
            "requested_by": "U9",
        },
        "turn.completed": {"event_id": "ev-1", "outcome": "delivered"},
    }
    payload = json.dumps(
        {
            "version": REPLY_WIRE_VERSION,
            "event": event_name,
            "target": _target().model_dump(),
            **bodies[event_name],
        }
    )
    assert _EVENTS.validate_json(payload).event == event_name


def test_an_unmodelled_field_is_rejected() -> None:
    # ``extra="forbid"``, matching ``models.py``'s ``_STRICT``: a producer that
    # invents a field must fail loudly rather than have it silently ignored,
    # which is the ACI's own extra="ignore" hazard (events.py:38-48) that
    # T-A15 makes executable.
    payload = json.dumps(
        {
            "version": REPLY_WIRE_VERSION,
            "event": "reply.update",
            "target": _target().model_dump(),
            "text": "hi",
            "endpoint": "https://attacker.example",
        }
    )
    with pytest.raises(ValidationError):
        _EVENTS.validate_json(payload)


def test_the_target_rejects_an_unmodelled_field() -> None:
    with pytest.raises(ValidationError):
        ReplyTarget(
            kind="email",
            address="a@b.c",
            conversation_id="t-1",
            reply_ref=None,
            adapter="agentmail-sandbox",  # type: ignore[call-arg]
        )


def test_the_event_body_carries_no_route() -> None:
    # EB-B2: ``TargetRoute`` (endpoint + adapter) is a kwarg on ``emit``, never a
    # wire field -- a published event body must not tell an adapter where the
    # platform is sending it, and must never carry the egress credential
    # selector. This is the field-level half of D4.1.
    fields = set(
        ReplyUpdate.model_fields
        | TurnStatus.model_fields
        | ReplyPost.model_fields
        | TurnCompleted.model_fields
    )
    assert "endpoint" not in fields
    assert "adapter" not in fields
    assert "secret" not in fields
    assert "endpoint" not in ReplyTarget.model_fields
    assert "adapter" not in ReplyTarget.model_fields


def test_the_reply_ref_is_optional_and_opaque() -> None:
    # D2: the worker never parses it. A target with no ref at all is valid --
    # that is a post to a conversation that has no placeholder yet.
    assert _target(reply_ref=None).reply_ref is None
    # Nothing Slack-shaped is enforced: an AgentMail message id is as valid as
    # a Slack ts.
    assert _target(reply_ref="1720000000.000200").reply_ref == "1720000000.000200"


def test_the_wire_version_is_pinned_and_required() -> None:
    assert REPLY_WIRE_VERSION == "1.0"
    with pytest.raises(ValidationError):
        TurnStatus(  # type: ignore[call-arg]
            event="turn.status", target=_target(), status="working"
        )


def test_reply_ack_carries_the_adapter_minted_ref() -> None:
    # The ack is how ``reply.post`` returns the new message's handle (today's
    # ``post`` -> ``card_ts``, kernel.py:1313-1319). ``None`` is a legitimate
    # ack from an adapter whose channel has no addressable message id.
    assert ReplyAck(ref="msg_1").ref == "msg_1"
    assert ReplyAck(ref=None).ref is None


def test_the_reply_models_are_exported_in_the_committed_schema() -> None:
    # The drift gate (``test_schema_compat.py``) only sees models listed in
    # ``schema_export._MODELS``, so a new model that is never added there is
    # invisible to it -- the schema would stay "current" while the wire it
    # documents is missing half its surface. This asserts the export, not the
    # drift.
    rendered = json.loads(render_schema())
    defs = rendered.get("$defs", {})
    for name in (
        "ReplyTarget",
        "NavAffordance",
        "SettledOutcome",
        "TurnStatus",
        "ReplyUpdate",
        "ReplyPost",
        "TurnCompleted",
    ):
        assert name in defs, f"{name} is missing from the exported channel-protocol schema"
