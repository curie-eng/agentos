"""Worker-only current-authority check for durable human GitHub feedback."""

import uuid

from aci_protocol import QueuedTurn
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from ..auth import require_internal_worker_token
from ..config import get_settings
from ..deps import SessionDep
from ..github_review_events import FeedbackIgnored, FeedbackUnavailable
from ..github_review_store import verify_queued_feedback

router = APIRouter(
    prefix="/v1/internal/github/reviews",
    tags=["internal-github-reviews"],
    dependencies=[Depends(require_internal_worker_token)],
)


class ReviewVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn: QueuedTurn
    deployment_id: uuid.UUID


class ReviewVerificationOut(BaseModel):
    head_sha: str
    agent_id: uuid.UUID
    sender: str
    receipt: str


@router.post("/{event_id}/verify", response_model=ReviewVerificationOut)
async def verify_review(
    event_id: str,
    payload: ReviewVerificationRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> ReviewVerificationOut:
    response.headers["Cache-Control"] = "no-store"
    if event_id != payload.turn.event_id:
        raise HTTPException(
            409, {"code": "feedback_turn_mismatch"}, headers={"Cache-Control": "no-store"}
        )
    try:
        result = await verify_queued_feedback(
            session,
            payload.turn,
            payload.deployment_id,
            settings=get_settings(),
            client=request.app.state.http_client,
        )
    except FeedbackUnavailable as exc:
        raise HTTPException(
            503, {"code": exc.code}, headers={"Cache-Control": "no-store"}
        ) from None
    except FeedbackIgnored as exc:
        raise HTTPException(
            409, {"code": exc.code}, headers={"Cache-Control": "no-store"}
        ) from None
    return ReviewVerificationOut.model_validate(result)
