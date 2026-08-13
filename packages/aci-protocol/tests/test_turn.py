"""Reader-context propagation into nested models on the queued-turn payload,
and ``ReplyHandle``'s routing half (ADR-0096 phase 2, D1/D2).

``QueuedTurn`` crosses the Valkey Stream boundary between the dispatcher and the
worker, carrying a nested ``ReplyHandle``. The tolerant-read policy must reach
that nested model, not just the top-level one -- pydantic propagates validation
context into nested models, but decision 1 says assert it, do not assume it.

``ReplyHandle.kind`` is REQUIRED (plan section 1, D1): an optional kind forces
the resolver to answer "what do I do when kind is None?", and every honest answer
is an address-only fallback or a ``"slack"`` default -- #38's silent misroute
re-opened at the exact point the field that prevents it finally exists.
``ReplyHandle.adapter`` is optional at the schema (a third-party producer is not
rejected outright) but every first-party mint site sets it explicitly; the mint
sites are asserted by T-A17 (dispatcher), T-A18 (resume) and T-C2 (ingress).
"""

import json
from pathlib import Path

import pytest
from aci_protocol import QueuedTurn, ReplyHandle, parse_queued_turn
from aci_protocol.events import _READER_CONTEXT_KEY
from pydantic import ValidationError

# The committed cross-language golden the Rust CLI re-serializes byte-identically
# (`cli/src/queue.rs::queued_turn_matches_cross_language_golden`, T-A3). Read from
# disk rather than inlined, so this test and that one cannot drift apart.
_GOLDEN = (
    Path(__file__).resolve().parents[1] / "schema" / "queued-turn.fixture.json"
)


def _turn_payload_with_unknown_fields() -> dict[str, object]:
    return {
        "event_id": "e1",
        "conversation_id": "c1",
        "author": "u1",
        "text": "hi",
        "received_at": "2026-01-01T00:00:00Z",
        "future_top": 1,
        "reply_handle": {
            "kind": "slack",
            "channel": "C1",
            "placeholder": "1.0",
            "future_nested": 2,
        },
    }


def test_reader_context_reaches_nested_reply_handle() -> None:
    # With the reader context, an unknown field on the TOP-LEVEL turn and an
    # unknown field on the NESTED reply handle are both ignored -- proving the
    # context propagated into ReplyHandle.
    turn = QueuedTurn.model_validate(
        _turn_payload_with_unknown_fields(),
        context={_READER_CONTEXT_KEY: True},
    )
    assert turn.event_id == "e1"
    assert turn.reply_handle.channel == "C1"


def test_nested_reply_handle_is_strict_without_reader_context() -> None:
    # Producers stay strict: without the reader context, the unknown nested field
    # is rejected on construction/validation.
    with pytest.raises(ValidationError):
        QueuedTurn.model_validate(_turn_payload_with_unknown_fields())


def test_parse_queued_turn_tolerates_unknown_top_level_and_nested_fields() -> None:
    # The sanctioned consumer decode threads the reader context, so an unknown
    # field on the TOP-LEVEL turn and an unknown field on the NESTED reply handle
    # are both tolerated (the queue-boundary counterpart to the NDJSON decoders).
    turn = parse_queued_turn(json.dumps(_turn_payload_with_unknown_fields()))
    assert turn.event_id == "e1"
    assert turn.reply_handle.channel == "C1"


def test_constructing_queued_turn_with_unknown_field_is_strict() -> None:
    # Tolerance is read-only: producers construct the model directly, where an
    # unknown field is still rejected.
    with pytest.raises(ValidationError):
        QueuedTurn(
            event_id="e1",
            conversation_id="c1",
            author="u1",
            text="hi",
            reply_handle=ReplyHandle(kind="slack", channel="C1", placeholder="1.0"),
            received_at="2026-01-01T00:00:00Z",
            bogus=1,
        )


# --- T-A1: the routing half of the pair is required ---------------------------


def test_reply_handle_without_kind_is_rejected_on_construction() -> None:
    """T-A1 / AC1. A producer that omits `kind` is refused at the source.

    This is the assertion that forecloses the compatibility path: with `kind`
    optional, `ReplyHandle(channel=..., placeholder=...)` would construct fine
    and the resolver would have to invent a kind for it.
    """

    with pytest.raises(ValidationError):
        ReplyHandle(channel="C1", placeholder="1.0")


def test_a_kindless_payload_is_rejected_even_by_a_tolerant_consumer() -> None:
    """T-A1, the consumer half. Tolerance is about UNKNOWN fields, never about
    MISSING required ones: an old (0.2.x) producer's turn arriving at a 0.3.0
    worker must dead-letter loudly (plan section 6.4, E1), not decode with a
    fabricated kind. Without this, `extra="ignore"` plus a defaulted `kind`
    would let the pre-cutover shape through silently.
    """

    payload = _turn_payload_with_unknown_fields()
    handle = dict(payload["reply_handle"])  # type: ignore[arg-type]
    handle.pop("kind")
    payload["reply_handle"] = handle

    with pytest.raises(ValidationError):
        QueuedTurn.model_validate(payload, context={_READER_CONTEXT_KEY: True})


def test_the_committed_golden_fixture_carries_slack_as_its_kind() -> None:
    """T-A1, the golden half / AC1 and AC6.

    `schema/queued-turn.fixture.json` is the cross-language golden: the Rust CLI
    asserts byte-identical re-serialization of this exact file (T-A3), so it is
    regenerated with the model, never hand-edited. Reading it back through the
    sanctioned consumer decode proves the regeneration happened AND that the new
    field landed with the only honest value for a Slack-shaped fixture.
    """

    turn = parse_queued_turn(_GOLDEN.read_text())

    assert turn.reply_handle.kind == "slack"
    assert turn.reply_handle.channel == "C0GOLDENAAA"
    assert turn.reply_handle.placeholder == "1720000000.000200"


def test_adapter_is_optional_and_round_trips_when_a_producer_sets_it() -> None:
    """T-A1, the `adapter` half (plan EB-A1, revision 4).

    `adapter` is the egress-credential selector a pre-resolution sink call needs
    (EB-B1's trust argument), so it must survive the wire, and it must be
    OPTIONAL at the schema so a third-party producer is not rejected outright.
    Both directions are asserted here: absent means None, present round-trips.
    """

    default = ReplyHandle(kind="slack", channel="C1", placeholder="1.0")
    assert default.adapter is None
    assert default.endpoint is None

    routed = ReplyHandle(
        kind="email",
        channel="agent@example.test",
        placeholder="msg_abc123",
        endpoint="http://curie-mail-adapter:8080/",
        adapter="agentmail-sandbox",
    )
    restored = ReplyHandle.model_validate_json(routed.model_dump_json())
    assert restored == routed
    assert restored.adapter == "agentmail-sandbox"
    assert restored.endpoint == "http://curie-mail-adapter:8080/"
