"""GitHub webhook receiver (J1).

Authenticated by the HMAC signature GitHub sends (not the platform API key), so
it lives outside the X-API-Key dependency. A push to the dev branch deploys, a
push to the prod branch promotes; other events are acknowledged and ignored.
"""

import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status

from ..config import get_settings
from ..deps import EvalQueueDep, SessionDep, StoreDep
from ..gitflow import log_push_outcome, process_push, verify_signature
from ..github_review_events import FeedbackIgnored, FeedbackUnavailable, parse_feedback
from ..github_review_store import admit_feedback
from ..schemas import WebhookResult
from ..wirebody import read_bounded_body

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["github"])


@router.post("/webhook", response_model=WebhookResult)
async def github_webhook(
    request: Request,
    session: SessionDep,
    store: StoreDep,
    eval_queue: EvalQueueDep,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> WebhookResult:
    settings = get_settings()
    body = await read_bounded_body(
        request, settings.github_webhook_max_body_bytes, subject="webhook body"
    )
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    if x_github_event == "ping":
        return WebhookResult(status="pong")
    is_review = x_github_event in {
        "issue_comment",
        "pull_request_review_comment",
        "pull_request_review",
    }
    if x_github_event != "push" and not is_review:
        return WebhookResult(status="ignored")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "webhook body is not valid JSON") from exc

    if is_review:
        if not settings.github_review_ingress_enabled:
            return WebhookResult(status="feedback_disabled")
        try:
            feedback = parse_feedback(x_github_event, payload, x_github_delivery)
            row, created = await admit_feedback(
                session,
                feedback,
                settings=settings,
                client=request.app.state.http_client,
                traceparent=request.headers.get("traceparent"),
            )
        except FeedbackUnavailable as exc:
            raise HTTPException(503, {"code": exc.code}, headers={"Retry-After": "10"}) from None
        except FeedbackIgnored as exc:
            if exc.code == "invalid_delivery":
                raise HTTPException(400, {"code": exc.code}) from None
            return WebhookResult(status="feedback_ignored", errors=[{"code": exc.code}])
        if not created:
            return WebhookResult(status="feedback_duplicate")
        # The committed row is the recovery authority if Valkey or this API
        # process fails between persistence, XADD and the response.
        await request.app.state.github_review_reconciler.reconcile_once(row.event_id)
        await session.refresh(row)
        return WebhookResult(status=f"feedback_{row.status}")

    result = await process_push(session, store, settings, eval_queue, payload)
    log_push_outcome(result, payload, source="github webhook")
    return result
