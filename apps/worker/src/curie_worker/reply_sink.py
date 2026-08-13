"""The neutral egress seam: one ``emit``, and the only place ``kind`` is switched.

The kernel holds ONE ``ReplySink`` and never asks what a channel can do (ADR-0096
D3). Adapter selection lives here, below the seam: ``ReplySinkRouter`` picks
``SlackReplyAdapter`` for ``kind == "slack"`` and ``HttpReplyAdapter`` for
everything else. A ``kind`` branch reappearing in ``kernel.py`` is the seam
leaking back upward.

``TargetRoute`` is a worker-local kwarg, never a wire field: a published event
body must not tell an adapter where the platform is sending it, and must never
carry the egress-credential selector. It is built from one of two SERVER-authored
sources -- the binding row (``BindingResolver.resolve``) on a resolved turn, or
the server-minted ``reply_handle`` on the pre-resolution paths -- and never from
adapter-supplied data.

Egress FAILS CLOSED (D4.3): an adapter with no configured credential, no adapter
identity, or no endpoint raises instead of sending anonymously. An unauthenticated
platform request would let any reachable pod be impersonated and would give the
adapter no way to tell the platform from an attacker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp
from channel_protocol.reply import ReplyAck, ReplyEvent
from pydantic import BaseModel, ConfigDict

from .config import WorkerConfig
from .slack_sink import SlackReplyAdapter

logger = logging.getLogger(__name__)

# The per-adapter egress credential travels in this header, and only this header.
ADAPTER_SECRET_HEADER = "X-Curie-Adapter-Secret"

SLACK_KIND = "slack"

# The most acknowledgement body the worker will read off an adapter. The ack
# carries one optional ``ref`` string, so 64 KiB is orders of magnitude more than
# any honest adapter needs -- and the ceiling matters because the endpoint is
# operator-configured and answers on the worker's own heap: an unbounded read of
# a hostile (or merely broken) adapter's response is a worker OOM per turn, and
# the worker holds the kernel's locks. Oversize is a DELIVERY FAILURE, not a
# truncation: a body this large is not a reply the platform can interpret, and
# silently reading its first 64 KiB would hand ``_ref_from`` a half-parsed handle.
MAX_ACK_BODY_BYTES = 64 * 1024

# "Unreachable" for the HTTP path: the endpoint's HOST did not answer. A response
# that arrived and said no (any status) is NOT an unreachability -- it must stay
# loud even on a best-effort turn, or a rejecting adapter reads as a delivered one.
_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectionError,
    asyncio.TimeoutError,
)


def _redacted(endpoint: str | None) -> str:
    """An endpoint reduced to scheme and host, for a message a human will read.

    An endpoint can carry a token in its path or query, and these messages reach
    worker logs, dead-letter rows and tickets. Scheme plus host is enough for an
    operator to recognize which adapter is meant. Same shape as the API side's
    helper (``alembic/versions/0024_...``), deliberately: one redaction form
    across the two halves of the binding route.
    """

    if not endpoint:
        return "unset"
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.hostname:
        return "an unparseable endpoint"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class MissingAdapterCredentialError(RuntimeError):
    """Platform egress had no credential (or no target) and sent nothing."""


class OversizedAdapterResponseError(RuntimeError):
    """The adapter's acknowledgement body exceeded ``MAX_ACK_BODY_BYTES``.

    A delivery FAILURE, deliberately: the turn is retried or dead-lettered like
    any other rejected response rather than being recorded as delivered off a
    body the worker refused to finish reading.
    """


class RedirectedAdapterEndpointError(RuntimeError):
    """The adapter endpoint answered with a redirect, which is NOT followed.

    Following it would re-send the request -- and the per-adapter egress secret
    in ``X-Curie-Adapter-Secret`` -- to whatever origin the redirect named, so a
    compromised or misconfigured adapter could bounce the platform's credential
    anywhere. A redirect is therefore a delivery FAILURE, loud like any other
    rejected response, never a hop.
    """


class TargetRoute(BaseModel):
    """Where this turn's replies are delivered, and under whose credential.

    Deliberately NOT on the wire (EB-B2). ``endpoint`` is the adapter's
    server-controlled ingress URL; ``adapter`` is the operator-chosen slug that
    selects the per-adapter egress secret (D4.2). Both are None for a Slack turn
    on the worker's configured transport.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str | None = None
    adapter: str | None = None


class ReplySink(Protocol):
    """The kernel's whole view of egress: one verb, four events."""

    async def emit(
        self,
        event: ReplyEvent,
        *,
        route: TargetRoute,
        best_effort_unreachable: bool = False,
    ) -> ReplyAck: ...


class HttpReplyAdapter:
    """Delivers neutral JSON events to a binding's server-controlled endpoint.

    One POST per event, authenticated with the per-adapter secret. No transport
    fallback of any kind (E7): ``_with_transport_fallback``'s target is the
    worker's DEFAULT SLACK transport, so falling back here would post an email
    reply into a Slack workspace -- the exact hazard #530's issue text flagged.

    ``best_effort_unreachable`` (#708) is honored by logging and returning, with
    no fallback target, so an approval-resume turn whose adapter has died ACKs
    instead of dead-lettering. It covers an UNREACHABLE target only: a missing
    credential still raises, because fail-closed outranks best-effort.
    """

    def __init__(self, credentials: Mapping[str, str], *, timeout_s: float = 30.0) -> None:
        self._credentials = dict(credentials)
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    def _secret_for(self, route: TargetRoute) -> tuple[str, str]:
        """The (endpoint, secret) pair, or raise having sent nothing."""
        if not route.endpoint:
            # E17: a non-Slack binding with no endpoint is an operator error.
            raise MissingAdapterCredentialError(
                "platform egress has no endpoint for this turn; refusing to deliver"
            )
        if not route.adapter:
            raise MissingAdapterCredentialError(
                f"platform egress to {_redacted(route.endpoint)} has no adapter identity; "
                "refusing to send an unauthenticated request"
            )
        secret = self._credentials.get(route.adapter)
        if not secret:
            raise MissingAdapterCredentialError(
                f"no egress credential configured for adapter {route.adapter!r}; "
                "refusing to send an unauthenticated request"
            )
        return route.endpoint, secret

    async def emit(
        self,
        event: ReplyEvent,
        *,
        route: TargetRoute,
        best_effort_unreachable: bool = False,
    ) -> ReplyAck:
        endpoint, secret = self._secret_for(route)
        body = event.model_dump_json()
        headers = {"Content-Type": "application/json", ADAPTER_SECRET_HEADER: secret}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                # ``allow_redirects=False``: aiohttp's default replays the request,
                # secret header and all, at the Location the ENDPOINT chose, so a
                # redirecting adapter would be a cross-origin credential-exfil
                # primitive. Refuse the redirect instead of following it.
                async with session.post(
                    endpoint, data=body, headers=headers, allow_redirects=False
                ) as response:
                    if 300 <= response.status < 400:
                        raise RedirectedAdapterEndpointError(
                            f"adapter endpoint {_redacted(endpoint)} answered "
                            f"{response.status} (redirect); refusing to re-send the "
                            "egress credential to the redirect target"
                        )
                    response.raise_for_status()
                    # Read the cap plus one byte, never ``.text()``: the whole
                    # point is that the body is never fully buffered, and the
                    # extra byte is what tells "exactly at the cap" (fine) from
                    # "there is more coming" (refused). Content-Length is not
                    # consulted -- it is absent on a chunked response and is the
                    # attacker's own number besides.
                    payload = await response.content.read(MAX_ACK_BODY_BYTES + 1)
                    if len(payload) > MAX_ACK_BODY_BYTES:
                        raise OversizedAdapterResponseError(
                            f"adapter endpoint {_redacted(endpoint)} answered with more than "
                            f"{MAX_ACK_BODY_BYTES} bytes; refusing to buffer it and "
                            "treating the delivery as failed"
                        )
        except _UNREACHABLE_ERRORS as exc:
            if best_effort_unreachable:
                logger.warning(
                    "%s: adapter endpoint %s is unreachable (%s); completing the turn "
                    "best-effort without delivering the event",
                    event.event,
                    _redacted(endpoint),
                    exc,
                )
                return ReplyAck(ref=None)
            raise
        return ReplyAck(ref=_ref_from(payload))


def _ref_from(payload: bytes) -> str | None:
    """The adapter-minted handle off its response, if it minted one.

    A channel with nothing addressable to hand back (email has no editable
    message) answers with no ``ref`` at all, and the kernel must not care.
    """
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    ref = decoded.get("ref")
    return str(ref) if isinstance(ref, str) and ref else None


class ReplySinkRouter:
    """Picks the adapter for an event's ``kind``. The ONLY switch on kind."""

    def __init__(self, *, adapters: Mapping[str, ReplySink], default: ReplySink) -> None:
        self._adapters = dict(adapters)
        self._default = default

    async def emit(
        self,
        event: ReplyEvent,
        *,
        route: TargetRoute,
        best_effort_unreachable: bool = False,
    ) -> ReplyAck:
        sink = self._adapters.get(event.target.kind, self._default)
        return await sink.emit(
            event, route=route, best_effort_unreachable=best_effort_unreachable
        )


def build_reply_sink(config: WorkerConfig) -> ReplySinkRouter:
    """The worker's sink: Slack below its own origin, everything else over HTTP."""
    return ReplySinkRouter(
        adapters={
            SLACK_KIND: SlackReplyAdapter(
                config.slack_bot_token,
                base_url=config.slack_api_base_url or None,
                trusted_origins=config.slack_trusted_origins,
            )
        },
        default=HttpReplyAdapter(config.adapter_credentials),
    )
