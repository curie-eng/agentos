"""T-A15 / AC10d: an old (pre-0.3.0) consumer provably DROPS `kind`.

This is the executable form of the hazard that orders the cutover (plan section
6.1, step 3 before migration 0023). The claim under test is not "old pods are
stale"; it is that an old pod reading a NEW turn keeps routing on `address`
alone, and once 0023 permits two kinds to share one address, that is a silent
misroute to the wrong agent -- not a dead-letter, not an error, not anything an
operator would see.

Why a pinned fixture and a re-declared model rather than a live old pod: the
0.2.x model is what has to be held still. A test that imported the current
`ReplyHandle` would silently start asserting about the NEW shape the moment the
field lands, and go green while proving nothing. So the 0.2.x shape is committed
as `fixtures/reply_handle_0_2_9.json` and re-declared here (three fields, the
same `_AciModel` base the real 0.2.9 model used), and the fixture is what proves
the re-declaration is faithful.

Mutation that must fail this test: change `_ReplyHandle_0_2_9`'s config to
`extra="forbid"` and the first assertion fails -- which is the point. The
tolerance (`events.py:38-48`, `extra="ignore"` for consumers) is exactly what
creates the hazard: the old pod does not reject the new payload, it accepts it
minus the routing half.
"""

import json
from pathlib import Path

from aci_protocol import ReplyHandle
from aci_protocol.events import READER_CONTEXT, _AciModel

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reply_handle_0_2_9.json"


class _ReplyHandle_0_2_9(_AciModel):  # noqa: N801 - the version IS the name
    """The `ReplyHandle` shape as of PROTOCOL_VERSION 0.2.9, pinned.

    Re-declared rather than imported on purpose: importing the live model would
    make this test follow the change it exists to measure. It inherits
    `_AciModel` so it carries the SAME reader-context/`extra="ignore"` policy the
    real 0.2.9 model carried -- the policy is the mechanism under test.
    """

    channel: str
    placeholder: str
    endpoint: str | None = None


def test_the_pinned_model_matches_the_committed_0_2_9_payload() -> None:
    """The re-declaration is faithful to the shape that actually shipped.

    Without this, the rest of the file could be asserting about a model nobody
    ever ran.
    """

    payload = json.loads(_FIXTURE.read_text())
    assert set(payload) == {"channel", "placeholder", "endpoint"}

    old = _ReplyHandle_0_2_9.model_validate(payload, context=READER_CONTEXT)
    assert old.channel == "C0GOLDENAAA"
    assert old.placeholder == "1720000000.000200"
    assert old.endpoint is None


def test_the_0_2_9_model_parses_a_0_3_0_payload_and_loses_the_kind() -> None:
    """T-A15, both halves.

    (a) Parsing SUCCEEDS: `extra="ignore"` under the reader context tolerates the
        new field, so nothing anywhere tells the old consumer it is out of date.
    (b) The parsed object has NO `kind` at all, so a resolver fed from it can
        only route on `address` -- the silent misroute, made executable.

    The 0.3.0 payload is built from the LIVE model rather than hand-written, so
    the test cannot drift from whatever shape the contract actually ships.
    """

    new_payload = json.loads(
        ReplyHandle(
            kind="email",
            channel="sandbox.theconnman@agentmail.to",
            placeholder="msg_abc123",
            endpoint="http://curie-mail-adapter:8080/",
            adapter="agentmail-sandbox",
        ).model_dump_json()
    )
    assert new_payload["kind"] == "email", "the 0.3.0 payload must carry the new field"

    old = _ReplyHandle_0_2_9.model_validate(new_payload, context=READER_CONTEXT)

    # (a) It parsed. No exception, no warning, no version gate -- `QueuedTurn`
    # has no `version` field at all (`ndjson.py:109-118`), which is why FU-7
    # exists and why this cutover is quiescent.
    assert old.channel == "sandbox.theconnman@agentmail.to"

    # (b) And the routing half is simply gone.
    assert not hasattr(old, "kind")
    assert "kind" not in old.model_fields
    assert "kind" not in old.model_dump()
    assert "adapter" not in old.model_dump()


def test_an_old_consumer_cannot_distinguish_two_kinds_at_one_address() -> None:
    """T-A15's consequence stated as behavior, not as prose.

    After migration 0023 widens uniqueness to `(kind, address)`, an email turn
    and a Slack turn can legitimately name the same address. To an old consumer
    the two payloads are INDISTINGUISHABLE, so whichever agent its address-only
    predicate finds first answers both. That is the misroute; asserting the
    equality is asserting the ambiguity.
    """

    shared_address = "C0SHARED01"
    slack_turn = json.loads(
        ReplyHandle(kind="slack", channel=shared_address, placeholder="1.0").model_dump_json()
    )
    email_turn = json.loads(
        ReplyHandle(
            kind="email",
            channel=shared_address,
            placeholder="1.0",
            endpoint="http://curie-mail-adapter:8080/",
            adapter="agentmail-sandbox",
        ).model_dump_json()
    )

    as_seen_by_old_slack = _ReplyHandle_0_2_9.model_validate(slack_turn, context=READER_CONTEXT)
    as_seen_by_old_email = _ReplyHandle_0_2_9.model_validate(email_turn, context=READER_CONTEXT)

    assert as_seen_by_old_slack.channel == as_seen_by_old_email.channel
    assert as_seen_by_old_slack.model_dump() == as_seen_by_old_email.model_dump(), (
        "an old consumer sees the two turns as the same routing key; only the "
        "cutover ordering (no old worker before 0023) prevents the misroute"
    )
