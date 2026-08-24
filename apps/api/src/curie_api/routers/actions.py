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

Ruling on an undo is NOT here. It is its own decision, with its own two refusal
rules, and it lands separately.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError

from .. import crud
from ..auth import require_api_key
from ..deps import SessionDep
from ..schemas import ActionComplete, ActionOut, ActionRecord

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
