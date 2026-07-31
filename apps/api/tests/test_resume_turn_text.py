"""The platform-authored resume-turn text (#1084).

``build_resume_turn`` is the one place an approver's note crosses from a durable
record into a model's context. The claim that "the note reaches the requester"
rests entirely on the interpolation in this function, and nothing in the tree
asserted it: the only other reference builds a turn to seed a dead-letter row
and never reads its text.

That is worth its own file rather than a passing assertion elsewhere. The text
is a contract with two consumers at once -- the model reading it as platform
voice, and a skill matching on its ``[approval resolved]`` prefix -- so a
wording change is a behavior change, and it should fail here rather than in a
deployment.

Pure unit tests over detached ORM instances: no session, no Valkey, nothing to
skip on.
"""

from __future__ import annotations

import uuid

from curie_api.models import Approval, ApprovalStatus
from curie_api.resumequeue import build_expiry_resume_turn, build_resume_turn


def _resolved(
    *,
    status: str = ApprovalStatus.approved,
    resolved_by: str | None = "U_MANAGER",
    note: str | None = None,
    summary: str = "Give ACME a 20% discount",
) -> Approval:
    """A detached, resolved ``Approval``: only the fields the builder reads."""

    return Approval(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        conversation_id="C1-thread-0",
        author="U_AE",
        summary=summary,
        reply_channel="C1",
        reply_placeholder="p-1",
        reply_endpoint=None,
        dedupe_key="ev-1",
        status=status,
        resolved_by=resolved_by,
        resolution_note=note,
    )


def test_a_noted_resolution_carries_the_note_into_the_resume_turn() -> None:
    turn = build_resume_turn(_resolved(note="approved for Q3"))

    # The exact clause, not just the substring: the space before and the period
    # after are what keep it a sentence rather than text jammed against the
    # attribution.
    assert " Note: approved for Q3." in turn.text
    # And the facts around it, so a rewrite cannot drop one while keeping the note.
    assert turn.text.startswith("[approval resolved]")
    assert '"Give ACME a 20% discount"' in turn.text
    assert "was approved by U_MANAGER" in turn.text


def test_a_bare_resolution_omits_the_note_clause_entirely() -> None:
    """No note means no ``Note:`` at all, not an empty one.

    An empty clause would read as "Note: ." to the model, which is a statement
    that the approver left a blank reason rather than none.
    """

    turn = build_resume_turn(_resolved(note=None))

    assert "Note:" not in turn.text
    assert "was approved by U_MANAGER" in turn.text


def test_a_rejection_says_rejected_and_still_carries_its_reason() -> None:
    """The clause is decision-independent, and a rejection is where it matters
    most: "rejected" without a reason is the half of the decision the requester
    cannot act on."""

    turn = build_resume_turn(
        _resolved(status=ApprovalStatus.rejected, note="discount exceeds policy")
    )

    assert "was rejected by U_MANAGER" in turn.text
    assert " Note: discount exceeds policy." in turn.text


def test_the_resume_turn_is_authored_by_the_resolver() -> None:
    """The author is the resolver, which is what lets the worker attribute a
    settled card without parsing the prose (#1084)."""

    turn = build_resume_turn(_resolved(note="fine"))
    assert turn.author == "U_MANAGER"


def test_an_expiry_turn_names_no_decider_and_no_note() -> None:
    """The sibling path: nobody decided, so there is nobody to attribute and
    nothing to quote. Asserted here so a shared edit to the two builders cannot
    leak an approver clause into the expiry wording."""

    turn = build_expiry_resume_turn(
        _resolved(status=ApprovalStatus.expired, resolved_by=None, note=None)
    )

    assert turn.text.startswith("[approval expired]")
    assert "Note:" not in turn.text
    assert turn.author == "system"
    # The instruction that makes an expiry safe: do not perform the gated action.
    assert "Do not perform the gated action." in turn.text
