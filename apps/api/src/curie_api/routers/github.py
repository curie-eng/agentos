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
from ..schemas import WebhookResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["github"])


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, rejecting anything over ``max_bytes`` early.

    The bound is enforced BEFORE the whole body is buffered, parsed, or
    authenticated (#633), so an unauthenticated oversized request cannot make
    the app hold an unbounded body in memory. A declared ``Content-Length`` over
    the bound is refused without reading a byte; then the body is read in chunks
    and refused the moment the accumulated size crosses the bound, so an absent
    or lying ``Content-Length`` (including a chunked/streamed request) is held to
    the same limit. Raises 413 on an oversized body.
    """

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            declared_len = -1  # unparseable: fall through to streamed enforcement
        if declared_len > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "webhook body exceeds the maximum size",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "webhook body exceeds the maximum size",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhook", response_model=WebhookResult)
async def github_webhook(
    request: Request,
    session: SessionDep,
    store: StoreDep,
    eval_queue: EvalQueueDep,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> WebhookResult:
    settings = get_settings()
    body = await _read_bounded_body(request, settings.github_webhook_max_body_bytes)
    if not verify_signature(
        settings.github_webhook_secret, body, x_hub_signature_256
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    if x_github_event == "ping":
        return WebhookResult(status="pong")
    if x_github_event != "push":
        return WebhookResult(status="ignored")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "webhook body is not valid JSON"
        ) from exc

    result = await process_push(session, store, settings, eval_queue, payload)
    log_push_outcome(result, payload, source="github webhook")
    return result
