"""The approval authorizer: who may resolve a pending approval (#246, #420).

This is the seam docs/interfaces/approval/INTERFACE.md calls the black line: a
server-side decision, at resolution time, of whether a given actor is allowed
to resolve a given pending approval. It runs HERE, on the server that owns the
durable ``Approval`` record, so it cannot be spoofed from inside the agent's
sandbox -- the runner and the bundle never participate in the decision.

This module is pure policy. It knows nothing about chat providers, route
bindings, or how an approver set is chosen: it is handed an
``approvers.ApproverSet``, which answers only "is this actor in the set", and it
applies every rule that is not membership. There is ONE authorizer, and the
swappable part is the set behind it (ADR-0034).

Self-approval is structural for ordinary session approvals: the actor who
authored the request is refused before a set is asked. A server-owned
``publication`` purpose is the sole exception. Its requester still passes
through the selected membership set, and the exception is recorded in evidence;
a caller cannot opt another approval into it.

Fail closed: a set that could not determine membership (a lookup that failed, a
binding the platform cannot read) denies. That is the set reporting
``undetermined``, and it stays distinct from reporting that the actor is not a
member -- a config or infrastructure error must not be rendered to a clicker as
policy, and must never widen an approver set an operator narrowed.

Each decision carries the evidence that produced it, so the audit row records
the authority that counted rather than only the actor. One honest limit on that
evidence: it proves the ASSERTED identity satisfied policy at click time, not
who actually clicked (the ADR-0033 residual, tracked separately).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .approvers import ApproverSet
from .models import Approval

# The one wording for a self-approval refusal, so the dispatcher's rendering of
# it does not depend on which set the approval happened to be bound to.
_SELF_APPROVAL_REASON = (
    "self-approval is blocked: the requester cannot resolve their own request"
)


@dataclass(frozen=True)
class AuthzDecision:
    """The verdict, the human-readable reason rendered to the clicker, and the
    membership facts that decided it (#420), which the audit row stores so the
    trail records the authority that counted rather than only the actor."""

    allowed: bool
    reason: str = ""
    evidence: dict[str, Any] | None = None


async def authorize_approval(
    approval: Approval,
    actor: str,
    actor_channel: str | None,
    *,
    approver_set: ApproverSet,
) -> tuple[str, AuthzDecision]:
    """Decide whether ``actor`` may resolve ``approval``, and name what decided
    (for the audit row).

    ``approver_set`` is the set this approval's route binding calls for, already
    chosen by an ``approvers.ApproverSetSelector``. Every binding maps to some
    set, including one the platform could not read, so there is no second path
    through here.

    The order is the contract: refuse ordinary self-approval BEFORE asking the
    set. A publication requester is intentionally checked by the set, because
    membership remains required even under that purpose-specific exception.
    """

    name = approver_set.audit_name

    publication_requester = (
        actor == approval.author and approval.purpose == "publication"
    )
    if actor == approval.author and not publication_requester:
        # Named after the set that would have decided, but carrying no evidence:
        # the set never ran, and recording its snapshot next to this denial would
        # imply membership was what refused.
        return name, AuthzDecision(allowed=False, reason=_SELF_APPROVAL_REASON)

    verdict = await approver_set.contains(actor, actor_channel)
    if verdict.undetermined or not verdict.member:
        # Both refuse, and the set says why: it is the only one that knows
        # whether it could not determine membership or the actor is genuinely
        # outside the set.
        return name, AuthzDecision(
            allowed=False, reason=verdict.reason, evidence=verdict.evidence
        )
    evidence = dict(verdict.evidence or {})
    if publication_requester:
        evidence["publication_requester_exception"] = True
    return name, AuthzDecision(allowed=True, evidence=evidence)
