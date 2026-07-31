"""The Slack output seam: edit the dispatcher's placeholder in place.

The dispatcher already posted a placeholder message and put its ``ts`` on the
queue. As the runner streams, the kernel edits that same message via
``chat.update`` (a throttled live edit), then writes the final text. This is the
one external service the kernel tests mock; everything else runs for real.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

import aiohttp
from channel_protocol import ConfirmIntent, OutboundMessage
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from .behaviorpacks import NavPack
from .blocks import approval_card, expired_approval_card, render, resolved_approval_card

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# "Unreachable" reply-endpoint errors (#530): the endpoint's HOST did not answer
# -- a dead CLI stub whose process exited, a wrong port, a network drop. These are
# aiohttp transport errors and asyncio timeouts, NOT SlackApiError: a SlackApiError
# means the endpoint IS reachable and answered with an error (auth/channel), which
# must keep its own text-only fallback and must NOT trigger a transport fallback.
_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
)


@dataclass(frozen=True)
class SettledCard:
    """How an approval ended, channel-neutrally, for the settled-card render.

    ``decision`` is None when nobody decided, which is the expiry case; a
    non-None value is the resolved case and brings ``resolver`` with it. The
    kernel builds this from the durable record, never from the platform-authored
    resume prose, so the card states what the record says.
    """

    requested_by: str
    decision: str | None = None
    resolver: str | None = None
    note: str | None = None


class SlackSink(Protocol):
    """Edit a message in place. The kernel throttles how often it calls this.

    ``endpoint`` is the per-turn reply target (a Slack Web API base URL) carried
    on the queue payload's reply handle (issue #19). ``None`` routes to the sink's
    configured default, so a real Slack workspace and a no-Slack CLI stub can
    each finalize their own turns on one worker.
    """

    async def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavPack | None = None,
        endpoint: str | None = None,
        best_effort_unreachable: bool = False,
    ) -> None: ...

    async def set_status(
        self, *, channel: str, thread_ts: str, status: str, endpoint: str | None = None
    ) -> None:
        """Set the assistant-thread status (the "shimmer" caption) on the thread.
        Best-effort so it never breaks a turn."""
        ...

    async def clear_status(
        self, *, channel: str, thread_ts: str, endpoint: str | None = None
    ) -> None:
        """Clear any assistant-thread status (the "shimmer") on the thread. A
        no-op when shimmering is off; best-effort so it never breaks a turn."""
        ...

    async def post(
        self,
        *,
        channel: str,
        message: OutboundMessage,
        requested_by: str,
        thread_ts: str | None = None,
        endpoint: str | None = None,
    ) -> str | None:
        """Post a NEW message (the approval card, #246), returning its ts.

        The kernel hands a channel-neutral ``OutboundMessage`` carrying a
        ``Confirm`` intent (ADR-0020); the adapter renders it into whatever
        widget the channel supports (Slack: the Block Kit approval card with
        Approve/Reject buttons). No channel-native markup crosses this seam.
        ``requested_by`` is the semantic actor id the adapter renders (Slack: a
        ``<@id>`` mention). Distinct from ``update``: the placeholder-edit reply
        model stays the norm; posting is reserved for platform-owned messages
        like the approval card. Raises on failure so the caller decides
        best-effort."""
        ...

    async def update_message(
        self,
        *,
        channel: str,
        ts: str,
        message: OutboundMessage,
        endpoint: str | None = None,
        settled: SettledCard | None = None,
    ) -> None:
        """Edit an already-posted platform message in place (settling an
        approval card: expired in #419, resolved in #1084).

        Like ``post``, the kernel hands a channel-neutral ``OutboundMessage``
        (ADR-0020) and the adapter renders the settled form. Distinct from
        ``update`` (which renders streamed reply Markdown): this replaces a known
        platform message. Best-effort at the call site.

        ``settled`` carries the outcome semantically -- who asked, what was
        decided, by whom, with what note -- and the adapter turns that into
        whatever the channel's settled card looks like. ``None`` keeps #419's
        behavior, the expired form, so an expiry caller needs no change.
        Deliberately a parameter rather than fields smuggled into
        ``OutboundMessage``: ``status`` there already means a rendered status
        line, and overloading it would give one field two meanings depending on
        which sink method received it."""
        ...


class AsyncSlackSink:
    """A SlackSink backed by the Slack Web API (chat.update).

    The constructor ``base_url`` is the worker's DEFAULT reply endpoint: unset
    leaves the SDK default (real Slack); set points the default at a stub. Each
    call may override it with a per-turn ``endpoint`` (issue #19), so replies
    route back to the ingress that enqueued the turn instead of a single
    worker-global endpoint. One ``AsyncWebClient`` is built and cached per distinct
    base URL, since the SDK binds the endpoint at client construction.
    """

    def __init__(self, token: str, *, base_url: str | None = None) -> None:
        self._token = token
        # Normalize "" to None so the default and an explicit-empty override map to
        # the same (real-Slack) client.
        self._default_base_url = base_url or None
        self._clients: dict[str | None, AsyncWebClient] = {}

    def _client_for(self, endpoint: str | None) -> AsyncWebClient:
        """The cached client for this turn's endpoint, or the worker default.

        A per-turn ``endpoint`` overrides the default; ``None`` (or empty) uses the
        default. Clients are cached per base URL because the SDK binds the endpoint
        at construction, so this never rebuilds a client for a repeat endpoint.
        """
        base_url = endpoint or self._default_base_url
        client = self._clients.get(base_url)
        if client is None:
            client = (
                AsyncWebClient(token=self._token, base_url=base_url)
                if base_url
                else AsyncWebClient(token=self._token)
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

    async def update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        nav: NavPack | None = None,
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
        rendered_text, blocks = render(text, nav)

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

    async def post(
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

    async def update_message(
        self,
        *,
        channel: str,
        ts: str,
        message: OutboundMessage,
        endpoint: str | None = None,
        settled: SettledCard | None = None,
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

    async def set_status(
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

    async def clear_status(
        self, *, channel: str, thread_ts: str, endpoint: str | None = None
    ) -> None:
        # Clear the assistant-thread status by setting it empty (Slack only
        # auto-clears on a posted message, and we edit the placeholder instead).
        await self.set_status(
            channel=channel, thread_ts=thread_ts, status="", endpoint=endpoint
        )
