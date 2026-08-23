"""Async HTTP client for the runner's ACI channel.

The runner (D1) exposes the ACI session over HTTP: ``POST /v1/event`` opens a turn
and streams outbound NDJSON to a ``final``; ``POST /v1/steer`` injects a follow-up
into the live turn (409 when no turn is active, the finish-race boundary the
kernel owns); ``POST /v1/interrupt`` hard-stops; ``GET /status`` reports turn
state. This client turns those into typed calls the kernel composes.

The turn is split into ``start_turn`` (awaits the response headers, at which point
the runner's turn is active) and iterating the returned ``TurnStream`` (the
NDJSON body). That split lets the kernel establish the active turn while holding
the per-thread lock, then release the lock and stream the body, so a concurrent
follow-up can only steer the live turn and never fork a second one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aci_protocol import Event, Interrupt, OutboundEvent, parse_ndjson_line
from curie_telemetry import inject_trace_headers
from curie_telemetry.attributes import sanitize_attributes
from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer, set_span_in_context

# The interrupt RPC is a control-plane POST, not a streaming turn (#742, a
# follow-up to #739): it exists only to hard-stop the live turn, never to carry
# a turn's output, so it must not inherit ``connect_timeout_s``/``total_timeout_s``,
# which are tuned for a long-running streamed turn (default 600s). A wedged
# runner that accepts the TCP connect and then answers nothing would otherwise
# hang every interrupt caller for up to that streaming budget. A healthy runner
# answers an interrupt well under a second. This bound lives here, at the RPC
# itself, so every caller inherits it for free; each caller then layers its own
# policy on top (``Kernel.release_thread`` swallows and releases,
# ``Kernel.interrupt_agent`` and the kill switch surface the failure and keep
# going) instead of re-deriving the bound -- or a coupling to this client's
# other timeouts -- at each call site.
_DEFAULT_INTERRUPT_TIMEOUT_S = 5.0
_NOOP_TRACER = trace.NoOpTracerProvider().get_tracer("curie-worker.runner-client")


def _auth_headers(token: str | None) -> dict[str, str] | None:
    """Per-call Authorization header for the per-sandbox runner token (issue #63).

    The ClientSession is worker-wide and dials many base_urls, so the token is a
    per-call header, never a session default -- a default would leak one sandbox's
    token to every other. Returns None (no header) when the token is unset/empty.
    """
    if token:
        return {"Authorization": f"Bearer {token}"}
    return None


class RunnerError(Exception):
    """The runner returned an unexpected HTTP status or an unreadable stream."""


def _set_attributes(span: Span, attributes: dict[str, object]) -> None:
    """Apply only the worker's closed telemetry vocabulary to ``span``."""

    for key, value in sanitize_attributes("curie-worker", attributes).items():
        span.set_attribute(key, value)


def _finish_span(span: Span, *, error: bool) -> None:
    span.set_status(Status(StatusCode.ERROR if error else StatusCode.OK))
    span.end()


class TurnStream:
    """An open ``/v1/event`` response: the turn is active; iterate for frames."""

    def __init__(self, response: aiohttp.ClientResponse, span: Span | None = None) -> None:
        self._response = response
        self._span = span or _NOOP_TRACER.start_span("POST runner")
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[OutboundEvent]:
        try:
            async for raw in self._response.content:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                yield parse_ndjson_line(line)
        except BaseException:
            # Never record the exception itself: a parse failure may carry the
            # malformed runner line, which is agent-controlled content.
            self._finish(error=True)
            raise
        else:
            self._finish(error=False)

    def close(self) -> None:
        """Abandon an unconsumed response, idempotently."""

        self._finish(error=True)

    def _finish(self, *, error: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.release()
        finally:
            _finish_span(self._span, error=error)

    async def __aenter__(self) -> TurnStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        # Normal EOF already closed the owner as OK inside ``__aiter__``.  If
        # the context exits while it is still open, the response was abandoned
        # (with or without a caller exception) and must be ERROR.
        self._finish(error=True)


class RunnerClient:
    """Dials a claimed runner over its base_url. One client serves all threads."""

    def __init__(
        self,
        *,
        connect_timeout_s: float = 10.0,
        total_timeout_s: float = 600.0,
        interrupt_timeout_s: float = _DEFAULT_INTERRUPT_TIMEOUT_S,
        session: aiohttp.ClientSession | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._own_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=total_timeout_s, connect=connect_timeout_s, sock_read=total_timeout_s
            )
        )
        # A per-request override, not folded into the session default above: it
        # replaces (not merges with) the session timeout for this one call, so
        # ``/v1/interrupt`` gets its own short control-plane budget regardless of
        # how the streaming timeouts above are tuned.
        self._interrupt_timeout = aiohttp.ClientTimeout(total=interrupt_timeout_s)
        # The composition root injects its configured tracer into the runs lane.
        # Defaulting to a private no-op (rather than the ambient global provider)
        # prevents this shared client from silently instrumenting the eval lane.
        self._tracer = tracer or _NOOP_TRACER

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        token: str | None = None,
        json: object | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> tuple[aiohttp.ClientResponse, Span]:
        """Send one request under a manually-owned CLIENT span.

        Authorization and W3C context share the wire header map, but only the
        closed method/address/port/status attributes are exported.  Using the
        dedicated Trace Context propagator means baggage can never cross.
        """

        parsed = urlsplit(base_url)
        attributes: dict[str, object] = {
            "http.request.method": method,
            # The worker talks only to its claimed runner. Exporting live pod or
            # container addresses adds high-cardinality deployment identifiers
            # without adding causal information.
            "server.address": "runner",
        }
        if parsed.port is not None:
            attributes["server.port"] = parsed.port
        span = self._tracer.start_span(
            f"{method} runner",
            kind=SpanKind.CLIENT,
            attributes=sanitize_attributes("curie-worker", attributes),
            record_exception=False,
            set_status_on_exception=False,
        )
        headers = _auth_headers(token) or {}
        span_context = set_span_in_context(span)
        context_token = attach(span_context)
        try:
            inject_trace_headers(headers)
            kwargs: dict[str, Any] = {"headers": headers or None}
            if json is not None:
                kwargs["json"] = json
            if timeout is not None:
                kwargs["timeout"] = timeout
            response = await self._session.request(
                method,
                f"{base_url}{path}",
                **kwargs,
            )
        except BaseException:
            _finish_span(span, error=True)
            raise
        finally:
            detach(context_token)
        _set_attributes(span, {"http.response.status_code": response.status})
        return response, span

    async def start_turn(
        self, base_url: str, event: Event, token: str | None = None
    ) -> TurnStream:
        """Open a turn. Returns once the runner has accepted it (turn active)."""
        resp, span = await self._request(
            "POST",
            base_url,
            "/v1/event",
            json=event.model_dump(),
            token=token,
        )
        if resp.status != 200:
            try:
                body = await resp.text()
            finally:
                resp.release()
                _finish_span(span, error=True)
            raise RunnerError(f"/v1/event -> {resp.status}: {body}")
        return TurnStream(resp, span)

    async def steer(self, base_url: str, event: Event, token: str | None = None) -> bool:
        """Inject a follow-up into the live turn. False on 409 (no active turn)."""
        resp, span = await self._request(
            "POST",
            base_url,
            "/v1/steer",
            json=event.model_dump(),
            token=token,
        )
        try:
            async with resp:
                if resp.status == 409:
                    result = False
                elif resp.status != 200:
                    body = await resp.text()
                    raise RunnerError(f"/v1/steer -> {resp.status}: {body}")
                else:
                    result = True
        except BaseException:
            _finish_span(span, error=True)
            raise
        _finish_span(span, error=False)
        return result

    async def interrupt(self, base_url: str, reason: str, token: str | None = None) -> None:
        """Hard-stop the live turn; its final is reclassified to idle.

        Bounded to ``_DEFAULT_INTERRUPT_TIMEOUT_S`` (or the constructor
        override), never the streaming ``total_timeout_s``/``sock_read``
        budget (#742): a wedged runner that accepts the connect and then
        answers nothing must not cost the caller up to that streaming budget
        just to find out. Raises ``asyncio.TimeoutError`` on expiry, same as
        any other failure here -- callers already decide per call site whether
        to swallow-and-fallback or surface-and-continue."""
        frame = Interrupt(reason=reason)
        resp, span = await self._request(
            "POST",
            base_url,
            "/v1/interrupt",
            json=frame.model_dump(),
            token=token,
            timeout=self._interrupt_timeout,
        )
        try:
            async with resp:
                if resp.status not in (200, 409):
                    body = await resp.text()
                    raise RunnerError(f"/v1/interrupt -> {resp.status}: {body}")
        except BaseException:
            _finish_span(span, error=True)
            raise
        _finish_span(span, error=False)

    async def reset(self, base_url: str, token: str | None = None) -> None:
        """Discard the runner's conversation so the next turn starts fresh (#550).

        The eval driver calls this between cases to enforce per-case isolation.
        A 409 (a turn is still active) is surfaced as a ``RunnerError`` like any
        other unexpected status: the eval flow is sequential, so a turn should
        never be live at reset time -- a 409 here is a real ordering bug, not a
        condition to swallow.
        """
        resp, span = await self._request("POST", base_url, "/v1/reset", token=token)
        try:
            async with resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RunnerError(f"/v1/reset -> {resp.status}: {body}")
        except BaseException:
            _finish_span(span, error=True)
            raise
        _finish_span(span, error=False)

    async def status(self, base_url: str) -> dict[str, object]:
        resp, span = await self._request("GET", base_url, "/status")
        try:
            async with resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RunnerError(f"/status -> {resp.status}: {body}")
                data: dict[str, object] = await resp.json()
        except BaseException:
            _finish_span(span, error=True)
            raise
        _finish_span(span, error=False)
        return data

    async def close(self) -> None:
        if self._own_session:
            await self._session.close()

    async def __aenter__(self) -> RunnerClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
