"""The channel ingress API (ADR-0096 phase 2, #1459).

Two endpoints, and every request/response model they use lives here rather than
in ``schemas.py``:

- ``POST /channels/token`` (platform key only) mints a ``chn`` token over a
  binding ROW's id plus its current generation, so an ingress adapter holds a
  credential scoped to exactly one binding instead of the platform key.
- ``POST /channels/turns`` (platform key OR a ``chn`` token) enqueues a
  ``QueuedTurn`` for the binding named in the BODY.

**Kind and address ride in the BODY, not the path.** An address is an opaque
routing key matched on equality; it may contain ``@``, ``.``, ``/``, ``?`` or
``#``, and a path segment silently 404s on the ``/`` case instead of failing
loudly.

**``X-API-Key``, not ``Authorization: Bearer``.** Every authenticated router in
this API reads ``X-API-Key`` (``auth.py``); one header, one parser, and the
wrong one is simply absent.

**The platform mints the turn; the request never routes itself.** ``kind``,
``endpoint`` and ``adapter`` come from the binding row loaded during token
validation, never from the request body (plan D4.1). Accepting an endpoint from
the wire would let one token point the platform's AUTHENTICATED egress at any
URL, which is credential capture rather than merely SSRF.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import redis.asyncio as redis
from aci_protocol import STREAM_PAYLOAD_FIELD, QueuedTurn, ReplyHandle
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .. import channel_token
from ..auth import require_api_key, verify_platform_key
from ..channel_token import CHANNEL_ENQUEUE_SCOPE
from ..config import get_settings
from ..deps import SessionDep
from ..models import AgentChannel
from ..schemas import ChannelBinding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

# The kind whose reply route is legitimately implicit: Slack replies go through
# the worker's CONFIGURED Slack origin (a wire-supplied endpoint would hand the
# bot token to whatever URL a turn named), so an unset endpoint/adapter pair is
# COMPLETE for `slack` and half-configured for anything else.
_IMPLICIT_ROUTE_KIND = "slack"

# The one detail string every ingress auth failure returns, identical for "no
# credential" and "wrong credential" -- the same string `require_state_access`
# uses. A caller that can tell the two apart (or spot a stale generation) can
# enumerate live bindings and probe rebinds.
_AUTH_DETAIL = "missing or invalid credential"

# The delivery claim key, per BINDING and delivery: `event_id` is derived from
# `(channel_id, delivery_id)` and never `delivery_id` alone, so two adapters
# sharing an upstream id space -- two AgentMail inboxes, two webhook sources --
# cannot swallow each other's turns (E16).
_CLAIM_PREFIX = "curie:channel"

# The owner-checked enqueue, as ONE script so the ownership check and the XADD
# are a single atomic step (Valkey runs a script single-threaded, which is
# exactly the guarantee needed here). Three exhaustive outcomes:
#
#   (a) we still own the lease        -> XADD, and the claim becomes the receipt
#   (b) the lease lapsed, NO successor -> re-claim and XADD in-script
#   (c) a foreign token or a stream id -> no XADD; name the current owner
#
# (b) is not a nicety: without it a winner that resumed after its lease expired
# but BEFORE any successor claimed would find an empty key, take the lease-lost
# arm, and report a duplicate for a delivery that was NEVER enqueued -- and the
# adapter retries only transport failures, so that delivery is silently dropped.
# (c)'s TOKEN comparison is what makes the lease OWNED: a merely SLOW winner
# whose lease was re-claimed must not XADD on top of the retry's entry.
_ENQUEUE_SCRIPT = """
local cur = redis.call('GET', KEYS[1])
if cur == ARGV[1] then
  local id = redis.call('XADD', KEYS[2], '*', ARGV[3], ARGV[2])
  redis.call('SET', KEYS[1], id, 'EX', ARGV[5])
  return {1, id}
elseif not cur then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
  local id = redis.call('XADD', KEYS[2], '*', ARGV[3], ARGV[2])
  redis.call('SET', KEYS[1], id, 'EX', ARGV[5])
  return {1, id}
else
  return {0, cur}
end
"""


# --- request/response models --------------------------------------------------


class ChannelTokenRequest(ChannelBinding):
    """Mint request: the binding pair to scope the token to, plus its lifetime.

    Subclasses `ChannelBinding` so the SAME `_validate_channel_binding` the
    agents API runs judges the pair here (E11). Two write paths cannot drift
    into two rules -- that drift is how #143 happened -- and an operator reads
    the identical message from either endpoint.
    """

    # An hour by default; a week is the ceiling. The TTL and the generation are
    # the only two things standing in for the revocation list phase 2
    # deliberately does not build, so an unbounded lifetime would remove one of
    # them.
    ttl_s: int = Field(default=3600, gt=0, le=604800)


class ChannelTokenOut(BaseModel):
    token: str


class TurnIn(ChannelBinding):
    """One inbound delivery, as the adapter presents it.

    `extra="ignore"`, unlike its parent: the body deliberately does NOT model
    `endpoint` or `adapter` (plan D4.1), and naming them must CHANGE NOTHING
    rather than 422 -- the fields are simply not part of what a caller gets a
    say in. The platform reads both from the binding row.

    `delivery_id` is the adapter's own stable upstream identifier (AgentMail's
    `message_id`, a webhook's delivery id). The platform derives `event_id` from
    it deterministically, so an adapter that never saw the response converges by
    retrying rather than by enqueuing a second turn.
    """

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    delivery_id: str
    conversation_id: str
    author: str
    text: str
    reply_ref: str


class TurnAccepted(BaseModel):
    """The ingress receipt. `duplicate` says whether THIS request enqueued.

    `stream_id` is None only while another request holds the claim and has not
    enqueued yet (the 202 case): there is no id to hand back yet, and the
    adapter's retry loop already knows what "come back" means.
    """

    event_id: str
    stream_id: str | None
    duplicate: bool


# --- shared helpers -----------------------------------------------------------


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, rejecting anything over ``max_bytes`` early.

    The bound is enforced BEFORE the body is buffered, parsed, or authenticated,
    so an oversized body cannot make the app hold it in memory on a route an
    adapter reaches from outside the first-party network. A declared
    ``Content-Length`` over the bound is refused without reading a byte; then the
    body is read in chunks and refused the moment the accumulated size crosses
    the bound, so an absent (chunked), understated or unparseable
    ``Content-Length`` is held to the same limit.

    The same shape as ``routers/github.py``'s ``_read_bounded_body`` and
    ``routers/bundles.py``'s ``_read_bounded_upload``; each keeps its own message
    because the surface an operator is reading about differs.
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
                "channel turn body exceeds the maximum size",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "channel turn body exceeds the maximum size",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_turn(raw: bytes) -> TurnIn:
    """Validate the raw body as a `TurnIn`, reporting failures as FastAPI would.

    The handler reads the body itself (the size bound has to run before anything
    else touches it), so this restores the 422 an ordinary body parameter would
    have produced -- same `loc`, same message text, which is what lets T-C7
    compare the ingress's validation messages against the agents API's.
    """

    try:
        return TurnIn.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *error.get("loc", ()))}
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
        ) from exc


async def _resolve_binding(
    session: Any, kind: str, address: str
) -> AgentChannel | None:
    """The binding row for one `(kind, address)` pair, or None.

    The PAIR, never the address alone: since migration 0023 one address can be
    bound under two kinds, and resolving on the address would let one kind's
    adapter reach the other kind's agent.
    """

    row: AgentChannel | None = await session.scalar(
        select(AgentChannel).where(
            AgentChannel.kind == kind, AgentChannel.address == address
        )
    )
    return row


def _route_is_configured(row: AgentChannel) -> bool:
    """Whether this binding can actually deliver a reply.

    `slack` needs no per-binding route (D4.4). Every other kind needs both
    halves, and the DB CHECK guarantees they are both-or-neither, so testing one
    of them would be enough -- both are tested because the guarantee is the
    database's, not this function's.
    """

    if row.kind == _IMPLICIT_ROUTE_KIND:
        return True
    return row.endpoint is not None and row.adapter is not None


# --- POST /channels/token -----------------------------------------------------


@router.post(
    "/token", response_model=ChannelTokenOut, dependencies=[Depends(require_api_key)]
)
async def mint_channel_token(
    data: ChannelTokenRequest, session: SessionDep
) -> ChannelTokenOut:
    """Mint a `chn` token for one binding (platform key only).

    Platform-key-only on purpose: a `chn` token does exactly one thing, enqueue
    for the binding in its claims. If it could mint, a compromised adapter would
    defeat both the TTL and the generation -- the only two things standing in for
    a revocation list.

    The token claims the ROW's id and its CURRENT generation, so it dies both
    when the pair is deleted and recreated (a new row id) and when the binding is
    re-pointed or merely re-asserted (a bumped generation).
    """

    row = await _resolve_binding(session, data.kind, data.address)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no agent is bound to channel kind {data.kind!r} at address "
            f"{data.address!r}; bind one before minting a token for it",
        )
    if not _route_is_configured(row):
        # E17: refuse at MINT time, so the operator error surfaces at bind time
        # rather than as a fail-closed escalation mid-turn, in the worker, far
        # from the request that caused it.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the binding for kind {data.kind!r} at address {data.address!r} has "
            "no reply route: set its endpoint and adapter (PATCH the agent's "
            "channel) before minting a token, or its turns would be enqueued "
            "with nowhere to reply and no credential to reply with.",
        )
    token = channel_token.mint(
        get_settings().api_key,
        channel_id=str(row.id),
        generation=row.generation,
        scope=CHANNEL_ENQUEUE_SCOPE,
        exp=int(time.time()) + data.ttl_s,
    )
    return ChannelTokenOut(token=token)


# --- POST /channels/turns -----------------------------------------------------


def _sha16(delivery_id: str) -> str:
    return hashlib.sha256(delivery_id.encode()).hexdigest()[:16]


def _event_id(channel_id: uuid.UUID, delivery_id: str) -> str:
    return f"chn-{channel_id}-{_sha16(delivery_id)}"


def _claim_key(channel_id: uuid.UUID, delivery_id: str) -> str:
    return f"{_CLAIM_PREFIX}:delivery:{channel_id}:{_sha16(delivery_id)}"


def _authenticate(x_api_key: str | None, row: AgentChannel | None) -> None:
    """The ingress 401 matrix, in the order `require_state_access` fixed.

    The platform key first (operator/first-party); else, if the header is
    present at all, the `chn` token against the binding the BODY claimed. Every
    failure -- absent header, malformed token, another binding's token, a stale
    generation, an expired token, the right token in the wrong header -- raises
    the identical 401.
    """

    if verify_platform_key(x_api_key):
        return
    if (
        x_api_key is not None
        and row is not None
        and channel_token.verify(
            x_api_key,
            get_settings().api_key,
            channel_id=str(row.id),
            generation=row.generation,
            scope=CHANNEL_ENQUEUE_SCOPE,
        )
    ):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_AUTH_DETAIL)


def _mint_turn(row: AgentChannel, body: TurnIn) -> QueuedTurn:
    """Build the `QueuedTurn` from the BINDING ROW plus the delivery's content.

    Three fields the request gets no say in: `kind` (the routing half),
    `endpoint` (where the reply goes) and `adapter` (whose credential
    authenticates it). This is the only mint site that produces non-Slack turns,
    so leaving `adapter` unset here would leave every production email turn
    without an egress-credential selector on the pre-resolution path.
    """

    return QueuedTurn(
        event_id=_event_id(row.id, body.delivery_id),
        conversation_id=body.conversation_id,
        author=body.author,
        text=body.text,
        reply_handle=ReplyHandle(
            kind=row.kind,
            channel=row.address,
            placeholder=body.reply_ref,
            endpoint=row.endpoint,
            adapter=row.adapter,
        ),
        received_at=datetime.now(UTC).isoformat(),
    )


async def _claim_delivery(
    client: redis.Redis, key: str, owner: str, lease_s: int
) -> bool:
    """Claim one delivery for THIS request: `SET NX EX`, first writer wins.

    The structural sibling of the dispatcher's `already_enqueued` guard
    (`apps/dispatcher/src/curie_dispatcher/queue.py`'s `claim_event`), with two
    deliberate differences: the dispatcher DROPS a retry silently while ingress
    ANSWERS it (an HTTP caller needs a response), and the value here is an owner
    TOKEN that later becomes the stream id rather than a bare `1`, which is what
    makes that answer possible.

    `SET NX` is the only atomic step available: a read-then-write guard (the
    kernel's `is_done` check, read long before `mark_done` is written) admits two
    winners, which is why idempotency is settled here, at ingress.
    """

    return bool(await client.set(key, owner, nx=True, ex=lease_s))


def _as_str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _enqueue_owned(
    client: redis.Redis,
    *,
    key: str,
    stream: str,
    owner: str,
    payload: str,
    lease_s: int,
    receipt_ttl_s: int,
) -> tuple[bool, str]:
    """Run the owner-checked enqueue script; True with the stream id when THIS
    request enqueued, False with the current owner's value when it did not."""

    result: Any = await client.eval(
        _ENQUEUE_SCRIPT,
        2,
        key,
        stream,
        owner,
        payload,
        STREAM_PAYLOAD_FIELD,
        str(lease_s),
        str(receipt_ttl_s),
    )
    enqueued, current = result
    return bool(enqueued), _as_str(current)


def _duplicate(
    event_id: str, current: str, response: Response
) -> TurnAccepted:
    """Answer a delivery someone else owns, from what the claim key holds.

    A `pending:` value means another request is mid-flight and there is no stream
    id yet, so the answer is 202 ("come back"); anything else IS the stream id of
    the entry that request enqueued, so the answer is 200 with that id. Either
    way this request issues no XADD.
    """

    if current.startswith("pending:"):
        response.status_code = status.HTTP_202_ACCEPTED
        return TurnAccepted(event_id=event_id, stream_id=None, duplicate=True)
    return TurnAccepted(event_id=event_id, stream_id=current, duplicate=True)


@router.post("/turns", response_model=TurnAccepted)
async def ingest_turn(
    request: Request,
    response: Response,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> TurnAccepted:
    """Enqueue one inbound delivery for the binding the body names.

    Order is load-bearing. The size bound runs FIRST, before authentication and
    before JSON parsing, so an oversized body is refused without the server ever
    HMAC-verifying or deserializing it. The body is validated next because the
    credential is scoped to the binding the body CLAIMS -- there is nothing to
    verify a `chn` token against until the pair is known. Only then is the
    request authenticated, and only then does an unknown pair earn a 404: a
    caller with no credential learns nothing about which bindings exist.
    """

    settings = get_settings()
    raw = await _read_bounded_body(request, settings.channel_turn_max_body_bytes)
    body = _parse_turn(raw)
    row = await _resolve_binding(session, body.kind, body.address)
    _authenticate(x_api_key, row)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no agent is bound to channel kind {body.kind!r} at address "
            f"{body.address!r}",
        )

    turn = _mint_turn(row, body)
    client: redis.Redis = request.app.state.valkey
    key = _claim_key(row.id, body.delivery_id)
    owner = f"pending:{secrets.token_hex(16)}"
    payload = turn.model_dump_json()

    # Two attempts, not a loop: the second exists only for the narrow case where
    # the claim key expired between our failed `SET NX` and the `GET` that would
    # have named its owner. If that repeats, the honest answer is "someone is
    # mid-flight" -- never a second XADD.
    for _attempt in range(2):
        if await _claim_delivery(
            client, key, owner, settings.channel_delivery_lease_s
        ):
            enqueued, current = await _enqueue_owned(
                client,
                key=key,
                stream=settings.runs_stream,
                owner=owner,
                payload=payload,
                lease_s=settings.channel_delivery_lease_s,
                receipt_ttl_s=settings.channel_delivery_receipt_ttl_s,
            )
            if enqueued:
                logger.info(
                    "channel ingress enqueued event_id=%s stream_id=%s kind=%s",
                    turn.event_id,
                    current,
                    row.kind,
                )
                return TurnAccepted(
                    event_id=turn.event_id, stream_id=current, duplicate=False
                )
            # Branch (c): our lease was re-claimed while we were slow. The other
            # claimant owns the delivery; we do not enqueue on top of it.
            return _duplicate(turn.event_id, current, response)
        held = await client.get(key)
        if held is not None:
            return _duplicate(turn.event_id, _as_str(held), response)

    response.status_code = status.HTTP_202_ACCEPTED
    return TurnAccepted(event_id=turn.event_id, stream_id=None, duplicate=True)
