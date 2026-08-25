"""The action ledger: record what an agent did, and read it back (ADR-0117).

The worker has no database of its own -- it persists an approval by POSTing to
this API, and it records an action the same way. So the two ACI frames of one
side-effecting call arrive here as two requests: a create when the call was made,
and a completion when its result came back.

Both are idempotent, because the worker redelivers at least once (ADR-0013). A
replayed create adopts the record it already wrote; a replayed completion is
returned unchanged rather than overwriting the first account of the call. That
second one matters more than it looks: the prior state on a record is what a
restore replays, so a completion allowed to rewrite it moves the target of an
undo that has already been offered to a human.

Ruling on an undo IS here, and executing one is not. Nothing in the platform can
reach a connector, so this decides whether a restore is permitted and hands back
the call to make. Keeping the ruling and the execution apart is what makes
deferring the executor safe: a refusal is recorded and returned before anything
could act on it, so whichever executor lands cannot bypass the check by holding
the connector's address.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError

from .. import crud
from ..auth import require_api_key
from ..deps import SessionDep
from ..models import ActionAuditEntry, ActionStatus, AgentAction
from ..schemas import (
    ActionAuditOut,
    ActionComplete,
    ActionOut,
    ActionRecord,
    ActionRestore,
    ActionUndo,
    ActionUndoOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions", tags=["actions"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=ActionOut, status_code=status.HTTP_201_CREATED)
async def record_action(
    data: ActionRecord, session: SessionDep, response: Response
) -> ActionOut:
    """Record a side-effecting call; idempotent on ``dedupe_key``.

    Created ``pending``: the call was made and nothing has come back to say what
    it did, which is also the state a turn that dies mid-call leaves behind.
    """

    try:
        action = await crud.create_action(session, data)
    except IntegrityError as exc:
        await session.rollback()
        existing = await crud.get_action_by_dedupe_key(session, data.dedupe_key)
        if existing is None:  # raced with a delete; surface the conflict as-is
            raise HTTPException(
                status.HTTP_409_CONFLICT, "action violates a uniqueness constraint"
            ) from exc
        response.status_code = status.HTTP_200_OK
        return ActionOut.model_validate(existing)
    return ActionOut.model_validate(action)


@router.get("", response_model=list[ActionOut])
async def list_actions(
    session: SessionDep,
    conversation_id: str | None = None,
    agent_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[ActionOut]:
    """A conversation's actions, oldest first -- the order a receipt lists them."""

    actions = await crud.list_actions(
        session,
        conversation_id=conversation_id,
        agent_id=agent_id,
        limit=min(max(limit, 1), 200),
    )
    return [ActionOut.model_validate(a) for a in actions]


@router.get("/{action_id}", response_model=ActionOut)
async def get_action(action_id: uuid.UUID, session: SessionDep) -> ActionOut:
    action = await crud.get_action(session, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    return ActionOut.model_validate(action)


@router.post("/{action_id}/complete", response_model=ActionOut)
async def complete_action(
    action_id: uuid.UUID, data: ActionComplete, session: SessionDep
) -> ActionOut:
    """Record what the tool answered.

    ``prior_state`` and ``target`` are what a restore replays. A completion that
    carries neither produces a record that is not undoable, which is the honest
    answer for a connector that replied in prose -- nothing has to declare it.
    """

    action = await crud.get_action(session, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    completed = await crud.complete_action(session, action, data)
    return ActionOut.model_validate(completed)


@router.get("/{action_id}/audit", response_model=list[ActionAuditOut])
async def get_action_audit(action_id: uuid.UUID, session: SessionDep) -> list[ActionAuditOut]:
    """The action's audit trail, oldest first.

    A refused undo leaves no trace anywhere else -- the world did not change and
    the record did not move -- so this is the only place the reason survives.
    """

    action = await crud.get_action(session, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
    entries = await crud.list_action_audit(session, action_id)
    return [ActionAuditOut.model_validate(e) for e in entries]


async def _refuse(
    session: SessionDep,
    action: AgentAction,
    data: ActionUndo,
    *,
    kind: str,
    reason: str,
    code: int,
    evidence: dict[str, object] | None = None,
) -> None:
    """Record the refusal, then raise it.

    Committed before the exception, so the reason outlives the HTTP response
    that carried it. An operator who pressed undo and saw a red message has
    somewhere to look; an operator who never saw the message still does.
    """

    session.add(
        ActionAuditEntry(
            action_id=action.id,
            action=kind,
            actor=data.actor,
            actor_channel=data.actor_channel,
            authorizer="conflict-check",
            authorized=False,
            reason=reason,
            evidence=evidence,
        )
    )
    await session.commit()
    raise HTTPException(code, reason)


@router.post("/{action_id}/undo", response_model=ActionUndoOut)
async def undo_action(
    action_id: uuid.UUID, data: ActionUndo, session: SessionDep
) -> ActionUndoOut:
    """Rule on putting back what this action changed.

    A 200 authorizes a restore and names the call that performs it; every other
    outcome is a refusal that changed nothing. The refusals are ordered from the
    record's own state outward to the world, so the most specific true reason is
    the one the operator is told.

    Who may undo is not decided here yet. ADR-0117 decision 3 makes an undo
    require the authorization the forward action required and no more, which
    needs the gating approval recorded on the action; that lands separately.
    Until then this endpoint is authenticated like every other router and gated
    by the conflict rule alone.
    """

    action = await crud.get_action(session, action_id)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")

    if action.undone_at is not None:
        await _refuse(
            session,
            action,
            data,
            kind="refused_already_undone",
            reason="this action was already undone",
            code=status.HTTP_409_CONFLICT,
        )
    if action.status != ActionStatus.succeeded:
        await _refuse(
            session,
            action,
            data,
            kind="refused_unsuccessful",
            reason=(
                f"the call did not succeed (status {action.status}), so there is nothing "
                "known to reverse"
            ),
            code=status.HTTP_409_CONFLICT,
        )
    if action.prior_state is None or action.target is None:
        # The stated reason and the receipt's reason are the same sentence: the
        # connector's own words when it had them, and an honest fallback when it
        # did not.
        await _refuse(
            session,
            action,
            data,
            kind="refused_irreversible",
            reason=action.detail or "no recorded prior state, so this cannot be undone",
            code=status.HTTP_409_CONFLICT,
        )
    if data.observed_state is None:
        # Not an assumption of "unchanged". The platform cannot read the resource
        # itself, and a restore performed without looking is the blind restore
        # decision 4 exists to prevent.
        await _refuse(
            session,
            action,
            data,
            kind="refused_unobserved",
            reason="refusing to restore without the live state to compare against",
            code=status.HTTP_412_PRECONDITION_FAILED,
        )
    if action.post_state is None:
        await _refuse(
            session,
            action,
            data,
            kind="refused_uncomparable",
            reason=(
                "this call never reported what it left, so whether the world has moved "
                "since cannot be determined"
            ),
            code=status.HTTP_409_CONFLICT,
        )
    if data.observed_state != action.post_state:
        # The rule the feature lives on. Naming both states matters as much as
        # refusing: the operator has to see that their own fix is what stopped
        # this, rather than reading it as a platform malfunction.
        await _refuse(
            session,
            action,
            data,
            kind="refused_conflict",
            reason="the target changed after this action; refusing to restore over it",
            code=status.HTTP_409_CONFLICT,
            evidence={"left": action.post_state, "observed": data.observed_state},
        )

    assert action.target is not None and action.prior_state is not None  # narrowed above
    claimed = await crud.claim_action_undo(session, action, actor=data.actor)
    session.add(
        ActionAuditEntry(
            action_id=action.id,
            action="authorized",
            actor=data.actor,
            actor_channel=data.actor_channel,
            authorizer="conflict-check",
            authorized=True,
            evidence={"restoring": action.prior_state},
        )
    )
    await session.commit()
    await session.refresh(claimed)
    return ActionUndoOut(
        action=ActionOut.model_validate(claimed),
        restore=ActionRestore(target=action.target, prior_state=action.prior_state),
    )
