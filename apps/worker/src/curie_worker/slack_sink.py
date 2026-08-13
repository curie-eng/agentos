"""The Slack ADAPTER of the neutral reply wire: edit the placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel emits ``reply.update`` events and this
adapter turns each into a ``chat.update`` on that same message (a throttled live
edit), then writes the final text. This is the one external service the kernel
tests mock; everything else runs for real.

Everything Slack-shaped lives below ``emit``: the Web API method names, Block Kit
rendering, the assistant-thread "shimmer" caption, and the #530 transport
fallback. The kernel above it knows only ``ReplyEvent`` + ``TargetRoute``.

**A per-turn endpoint is honored only at the CONFIGURED Slack origin** (D4.4).
Before ADR-0096 phase 2 this class built ``AsyncWebClient(token=self._token,
base_url=<whatever the turn named>)``, so a turn carrying an attacker's endpoint
captured the platform bot token. An endpoint whose origin differs from
``slack_api_base_url`` (or from real Slack when that is unset) is now refused
with ``UntrustedSlackEndpointError`` before any request leaves the process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar, cast
from urllib.parse import urlsplit

import aiohttp
from channel_protocol import ConfirmIntent, OutboundMessage
from channel_protocol.reply import (
    NavAffordance,
    ReplyAck,
    ReplyEvent,
    ReplyPost,
    ReplyUpdate,
    SettledOutcome,
    TurnCompleted,
    TurnStatus,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from .behaviorpacks import NavPack
from .blocks import approval_card, expired_approval_card, render, resolved_approval_card

if TYPE_CHECKING:  # pragma: no cover - import cycle: reply_sink builds this adapter
    from .reply_sink import TargetRoute

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# The SDK's own default base URL, which is the trusted origin when the worker
# configures none. Kept as a literal so the trust check has a concrete origin to
# compare against rather than "whatever the SDK happens to pick".
REAL_SLACK_BASE_URL = "https://slack.com/api/"

_DEFAULT_PORTS = {"http": 80, "https": 443}


class UntrustedSlackEndpointError(RuntimeError):
    """A turn named a Slack endpoint outside the configured Slack origin.

    Refused loudly and BEFORE any request: the platform bot token authenticates
    every Slack call, so honoring an arbitrary per-turn base URL hands that token
    to whoever named it (D4.4, finding 8).
    """


def _origin(url: str) -> tuple[str, str, int]:
    """Scheme + host + port, so two URLs at different PATHS compare equal."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme, 0)
    return scheme, (parts.hostname or "").lower(), port


def _nav_pack(nav: NavAffordance | None) -> NavPack | None:
    """The wire affordance in the renderer's own vocabulary.

    ``blocks.render`` (and ``ensure_hub_button`` under it) is the platform-owned
    append policy and speaks ``NavPack``; the wire owns ``NavAffordance`` because
    ``behaviorpacks`` cannot cross into ``channel_protocol`` (finding 16). The
    map is here, at the render call, so the disabled form stays "absent on the
    wire" rather than "a pack with a false flag".
    """
    if nav is None:
        return None
    return NavPack(enabled=True, hub_label=nav.label, hub_command=nav.command)


# "Unreachable" reply-endpoint errors (#530): the endpoint's HOST did not answer
# -- a dead CLI stub whose process exited, a wrong port, a network drop. These are
# aiohttp transport errors and asyncio timeouts, NOT SlackApiError: a SlackApiError
# means the endpoint IS reachable and answered with an error (auth/channel), which
# must keep its own text-only fallback and must NOT trigger a transport fallback.
_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
)


class SlackReplyAdapter:
    """A ``ReplySink`` backed by the Slack Web API (chat.update).

    The constructor ``base_url`` is the worker's DEFAULT reply endpoint AND the
    only trusted Slack origin: unset leaves the SDK default (real Slack); set
    points the default at an explicitly configured dev stub. A turn may override
    the PATH within that origin (issue #19), so replies route back to the ingress
    that enqueued the turn instead of a single worker-global endpoint, but an
    endpoint at any other origin is refused (D4.4). One ``AsyncWebClient`` is
    built and cached per distinct base URL, since the SDK binds the endpoint at
    client construction.
    """

    def __init__(self, token: str, *, base_url: str | None = None) -> None:
        self._token = token
        # Normalize "" to None so the default and an explicit-empty override map to
        # the same (real-Slack) client.
        self._default_base_url = base_url or None
        self._trusted_origin = _origin(self._default_base_url or REAL_SLACK_BASE_URL)
        self._clients: dict[str | None, AsyncWebClient] = {}

    async def emit(
        self,
        event: ReplyEvent,
        *,
        route: TargetRoute,
        best_effort_unreachable: bool = False,
    ) -> ReplyAck:
        """Render one neutral event as the Slack call that expresses it.

        ``turn.completed`` has no Slack expression and deliberately sends
        nothing: on Slack the edited message IS the delivery, so a completion
        signal would be a second, contentless message in the thread. The event
        exists for channels (email) whose reply is only sent at the end.
        """
        endpoint = route.endpoint
        target = event.target
        if isinstance(event, TurnStatus):
            await self._set_status(
                channel=target.address,
                thread_ts=target.conversation_id or "",
                status=event.status,
                endpoint=endpoint,
            )
            return ReplyAck(ref=None)
        if isinstance(event, ReplyUpdate):
            if event.message is not None:
                await self._update_message(
                    channel=target.address,
                    ts=target.reply_ref or "",
                    message=event.message,
                    endpoint=endpoint,
                    settled=event.settled,
                )
                return ReplyAck(ref=target.reply_ref)
            await self._update(
                channel=target.address,
                ts=target.reply_ref or "",
                text=event.text or "",
                nav=event.nav,
                endpoint=endpoint,
                best_effort_unreachable=best_effort_unreachable,
            )
            return ReplyAck(ref=target.reply_ref)
        if isinstance(event, ReplyPost):
            ref = await self._post(
                channel=target.address,
                message=event.message,
                requested_by=event.requested_by,
                thread_ts=target.conversation_id,
                endpoint=endpoint,
            )
            return ReplyAck(ref=ref)
        if isinstance(event, TurnCompleted):
            return ReplyAck(ref=None)
        raise AssertionError(f"unmodelled reply event {event!r}")

    def _client_for(self, endpoint: str | None) -> AsyncWebClient:
        """The cached client for this turn's endpoint, or the worker default.

        A per-turn ``endpoint`` overrides the default; ``None`` (or empty) uses the
        default. Clients are cached per base URL because the SDK binds the endpoint
        at construction, so this never rebuilds a client for a repeat endpoint.

        An endpoint outside the configured Slack origin raises here, which is the
        single choke point every Slack call passes through -- so the refusal lands
        before the transport fallback, before any retry, and before any request
        leaves the process with the platform bot token attached (D4.4).
        """
        if endpoint and _origin(endpoint) != self._trusted_origin:
            raise UntrustedSlackEndpointError(
                f"refusing to send the Slack bot token to {endpoint!r}: its origin is "
                f"not the configured Slack origin "
                f"{self._default_base_url or REAL_SLACK_BASE_URL!r}"
            )
        base_url = endpoint or self._default_base_url
        client = self._clients.get(base_url)
        if client is None:
            # ``retry_handlers=[]`` disables the SDK's own connection-error
            # retry, because THIS class already owns the retry policy for an
            # unreachable endpoint: ``_with_transport_fallback`` re-sends on the
            # configured default (#530). Leaving the SDK's handler in place
            # re-dials the target we have just learned is dead before our
            # fallback gets a turn, so every dead-stub reply pays a doubled
            # attempt and the fallback lands one round trip later. A transient
            # blip against a reachable Slack still reclaims and retries at the
            # delivery layer, loudly and bounded (ADR-0039).
            client = (
                AsyncWebClient(token=self._token, base_url=base_url, retry_handlers=[])
                if base_url
                else AsyncWebClient(token=self._token, retry_handlers=[])
            )
            self._clients[base_url] = client
        return client

    async def _with_transport_fallback(
        self,
        endpoint: str | None,
        op: Callable[[AsyncWebClient], Awaitable[_T]],
        *,
        describe: str,
        best_effort_unreachable: bool = False,
    ) -> _T:
        """Run ``op`` against this turn's endpoint, falling back to the worker
        DEFAULT transport when that endpoint is UNREACHABLE (#530).

        The motivating case: an approval turn resumed long after the original
        ``local``/``cluster message`` command exited finalizes against the reply
        endpoint persisted on the durable ``Approval`` -- the CLI's throwaway stub,
        now dead. Rather than let the connection error propagate (and dead-letter
        the resume, losing the reply), retry the same call on the default transport.

        Guarded so a reply is never misdirected: the fallback fires only when the
        failing target was a real per-turn ``endpoint`` AND a default is configured
        AND it differs from that endpoint. A ``SlackApiError`` (endpoint reachable,
        call rejected) is not an unreachability and is never caught here.

        ``best_effort_unreachable`` (#708) covers the pure-offline case: on a
        resume turn (kernel gates it via ``_is_approval_resume``) whose reply
        endpoint is dead and where there is genuinely NO default configured at all
        (``self._default_base_url is None`` -- the pure-offline local loop), log
        and RETURN instead of re-raising, so the resolved approval's
        already-executed turn ACKs instead of dead-lettering. The reply is still
        durably captured in the transcript, so it is not lost. When a default IS
        configured, the swallow never fires: a per-turn endpoint takes the #530
        default fallback above, and a reply already targeting the configured
        default that is unreachable is a genuine outage that stays LOUD.

        Best-effort delivery intentionally covers BOTH resume flavors -- the
        ``[approval resolved]`` resolve path and the ``[approval expired]`` expiry
        path -- because both carry the same ``approval-<id>-resolved`` event_id the
        kernel matches with ``_is_approval_resume``. That shared coverage is
        deliberate and plan-ratified, not an oversight.
        """
        primary = self._client_for(endpoint)
        resolved = endpoint or self._default_base_url
        has_distinct_default = bool(
            endpoint and self._default_base_url and resolved != self._default_base_url
        )
        try:
            return await op(primary)
        except _UNREACHABLE_ERRORS as exc:
            if has_distinct_default:
                logger.warning(
                    "%s: reply endpoint %r is unreachable (%s); falling back to the "
                    "default Slack transport",
                    describe,
                    endpoint,
                    exc,
                )
                return await op(self._client_for(None))
            # The best-effort swallow (#708) fires ONLY in the pure-offline case
            # where there is genuinely NO configured default transport at all
            # (``self._default_base_url is None``). It must NOT key off
            # ``not has_distinct_default``: that is also False when the reply is
            # going over a CONFIGURED default (``endpoint`` is None or equals the
            # default), where the target is a real, reachable-in-principle Slack.
            # An unreachable-class error there is a genuine transient OUTAGE that
            # must stay LOUD (raise -> reclaim -> retry per ADR-0039), never be
            # silently swallowed and acked. A resumed turn that then hits ANOTHER
            # approval gate or fails posts via ``_pause_for_approval``/``_escalate``
            # with ``best_effort_unreachable=False`` and therefore stays loud
            # (dead-letters) even in this offline loop -- a second approval card or
            # an escalation must never be silently dropped; surfacing via
            # dead-letter is correct, and completing those offline is out of scope
            # for #708.
            if best_effort_unreachable and self._default_base_url is None:
                logger.warning(
                    "%s: reply endpoint %r is unreachable (%s) with no default "
                    "transport; completing the resume turn best-effort without "
                    "delivering the reply",
                    describe,
                    endpoint,
                    exc,
                )
                return cast(_T, None)
            raise

    async def _update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavAffordance | None = None,
        endpoint: str | None = None,
        best_effort_unreachable: bool = False,
    ) -> None:
        # The runner emits Markdown; Slack renders mrkdwn. ``render`` converts to
        # mrkdwn and, if the reply carries a complete ``curie-reply`` block,
        # returns Block Kit to render instead -- keeping this dialect/structure
        # knowledge at the real-Slack seam, out of the kernel. A half-streamed or
        # malformed block falls back to text, so partials never show raw JSON.
        # ``nav`` (the agent's hub-button pack, threaded from the kernel) appends
        # the no-dead-ends hub button to a structured reply; None leaves it be.
        rendered_text, blocks = render(text, _nav_pack(nav))

        async def op(client: AsyncWebClient) -> None:
            if blocks is not None:
                # Slack rejects the whole message if a block exceeds a limit we did
                # not clamp; fall back to a text-only update (on the SAME reachable
                # client) so the turn still completes and the stream entry is acked
                # instead of re-enqueuing the paid model turn.
                try:
                    await client.chat_update(
                        channel=channel, ts=ts, text=rendered_text, blocks=blocks
                    )
                except SlackApiError:
                    logger.warning(
                        "chat_update with blocks rejected for %s; retrying text-only", ts
                    )
                    await client.chat_update(channel=channel, ts=ts, text=rendered_text)
            else:
                await client.chat_update(channel=channel, ts=ts, text=rendered_text)

        await self._with_transport_fallback(
            endpoint,
            op,
            describe="chat_update",
            best_effort_unreachable=best_effort_unreachable,
        )

    async def _post(
        self,
        *,
        channel: str,
        message: OutboundMessage,
        requested_by: str,
        thread_ts: str | None = None,
        endpoint: str | None = None,
    ) -> str | None:
        # Render the channel-neutral message into Block Kit HERE, below the seam
        # (ADR-0020): the kernel emits a Confirm intent, the Slack adapter turns it
        # into the approval card's Approve/Reject buttons. A message with no
        # interaction degrades to a plain text post (the mandatory text fallback).
        intent = message.interaction
        if isinstance(intent, ConfirmIntent):
            text, blocks = approval_card(
                approval_id=intent.id,
                summary=message.text,
                requested_by=requested_by,
                # ADR-0020's semantic field, rendered by this adapter into the
                # note-dialog buttons (#1053). Reading it here rather than in the
                # kernel keeps the widget decision below the seam.
                allow_free_text=intent.allow_free_text,
            )
        else:
            text, blocks = message.text, None

        # A rejected Block Kit payload falls back to text-only, mirroring
        # ``update``: the card is the resolve UI, but a plain-text notice still
        # beats losing the message (the API resolve path works regardless).
        async def op(client: AsyncWebClient) -> AsyncSlackResponse:
            try:
                if blocks is not None:
                    return await client.chat_postMessage(
                        channel=channel, text=text, blocks=blocks, thread_ts=thread_ts
                    )
                return await client.chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts
                )
            except SlackApiError:
                if blocks is None:
                    raise
                logger.warning(
                    "chat_postMessage with blocks rejected; retrying text-only"
                )
                return await client.chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts
                )

        response = await self._with_transport_fallback(
            endpoint, op, describe="chat_postMessage"
        )
        ts = response.get("ts")
        return str(ts) if ts else None

    async def _update_message(
        self,
        *,
        channel: str,
        ts: str,
        message: OutboundMessage,
        endpoint: str | None = None,
        settled: SettledOutcome | None = None,
    ) -> None:
        # Render the settled approval card HERE, below the seam (ADR-0020): the
        # kernel hands the channel-neutral summary plus the semantic outcome, and
        # the adapter picks the Slack form. No decision means nobody made one,
        # which is the expiry form (#419); a decision means the resolved form
        # (#1084), rendered by the SAME function the dispatcher's click path is
        # pinned against, so an API resolve and a click settle a card alike.
        if settled is None or settled.decision is None:
            text, blocks = expired_approval_card(summary=message.text)
        else:
            text, blocks = resolved_approval_card(
                summary=message.text,
                requested_by=settled.requested_by,
                decision=settled.decision,
                resolver=settled.resolver or "",
                note=settled.note,
            )

        # A rejected Block Kit payload falls back to text-only, mirroring
        # ``post``/``update``: disabling the card is best-effort, but a plain-text
        # expiry notice still beats leaving live-looking buttons.
        async def op(client: AsyncWebClient) -> None:
            try:
                await client.chat_update(
                    channel=channel, ts=ts, text=text, blocks=blocks
                )
            except SlackApiError:
                logger.warning(
                    "card chat_update with blocks rejected for %s; retrying text-only",
                    ts,
                )
                await client.chat_update(channel=channel, ts=ts, text=text)

        await self._with_transport_fallback(
            endpoint, op, describe="chat_update(card)"
        )

    async def _set_status(
        self, *, channel: str, thread_ts: str, status: str, endpoint: str | None = None
    ) -> None:
        # Best-effort: a workspace without the assistant feature, or any transient
        # error, must never fail the turn.
        try:
            await self._client_for(endpoint).assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )
        except Exception as exc:  # noqa: BLE001 -- status set is best-effort
            logger.debug("assistant set_status skipped for %s: %s", thread_ts, exc)
