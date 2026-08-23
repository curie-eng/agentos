"""Regression guard for the RunnerClient turn-stream release contract that the
kernel's _consume relies on (verify-f1 coverage gap 1): the aiohttp response must
be released on every exit path -- normal completion and an exception mid-stream --
so a turn never leaks a connection. We spy on the response's release() because it
is what TurnStream.close (called from __aexit__) invokes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aci_protocol import Event, Final, SessionStatus, TextDelta
from aiohttp import web
from aiohttp.test_utils import TestServer
from curie_worker.runner_client import RunnerClient, RunnerError
from opentelemetry import baggage
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)

DONE = SessionStatus.DONE


def _event() -> Event:
    return Event(type="message", text="hi", user="U", ts="1")


def _spy_release(turn: Any) -> dict[str, int]:
    calls = {"n": 0}
    real = turn._response.release

    def spy() -> Any:
        calls["n"] += 1
        return real()

    turn._response.release = spy
    return calls


def test_turn_stream_released_on_normal_completion(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [TextDelta(text="x"), Final(text="done", status=DONE)]
            handle = await asyncio.to_thread(h.substrate.claim, "tS")
            client = RunnerClient(total_timeout_s=30.0)
            try:
                turn = await client.start_turn(handle.base_url, _event())
                calls = _spy_release(turn)
                async with turn:
                    async for _frame in turn:
                        pass
                assert calls["n"] >= 1  # released on normal exit
            finally:
                await client.close()

    asyncio.run(go())


def test_turn_stream_released_when_consumer_raises(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # A hanging turn: the body is not fully read, so aiohttp will not
            # auto-release on EOF -- only TurnStream.__aexit__ can release it.
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="x")]
            h.runner.tail = [Final(text="done", status=DONE)]
            handle = await asyncio.to_thread(h.substrate.claim, "tSraise")
            client = RunnerClient(total_timeout_s=30.0)
            try:
                turn = await client.start_turn(handle.base_url, _event())
                calls = _spy_release(turn)
                try:
                    async with turn:
                        raise RuntimeError("consumer blew up mid-stream")
                except RuntimeError:
                    pass
                assert calls["n"] >= 1  # released on the error path too
            finally:
                hold.set()
                await client.close()

    asyncio.run(go())


# --- Per-call Authorization header (issue #63) --------------------------------
# Against a REAL local aiohttp server that records each request's headers, so the
# assertion is on the actual bytes on the wire, not a mock of the client.


class _HeaderRecordingRunner:
    """Records the request headers seen on each ACI route."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [
                web.post("/v1/event", self._event),
                web.post("/v1/steer", self._steer),
                web.post("/v1/interrupt", self._interrupt),
            ]
        )
        self.headers: dict[str, dict[str, str]] = {}

    async def _event(self, request: web.Request) -> web.StreamResponse:
        self.headers["event"] = dict(request.headers)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        await resp.write((Final(text="ok", status=DONE).model_dump_json() + "\n").encode("utf-8"))
        await resp.write_eof()
        return resp

    async def _steer(self, request: web.Request) -> web.Response:
        self.headers["steer"] = dict(request.headers)
        return web.json_response({"ok": True})

    async def _interrupt(self, request: web.Request) -> web.Response:
        self.headers["interrupt"] = dict(request.headers)
        return web.json_response({"ok": True})


async def _drain(turn: Any) -> None:
    async with turn:
        async for _frame in turn:
            pass


def test_runner_client_sends_bearer_token_on_every_call() -> None:
    async def go() -> None:
        runner = _HeaderRecordingRunner()
        server = TestServer(runner.app)
        await server.start_server()
        port = server.port
        assert port is not None
        base_url = f"http://127.0.0.1:{port}"
        client = RunnerClient(total_timeout_s=30.0)
        try:
            turn = await client.start_turn(base_url, _event(), token="tok-1")
            await _drain(turn)
            await client.steer(base_url, _event(), token="tok-1")
            await client.interrupt(base_url, "stop", token="tok-1")

            assert runner.headers["event"].get("Authorization") == "Bearer tok-1"
            assert runner.headers["steer"].get("Authorization") == "Bearer tok-1"
            assert runner.headers["interrupt"].get("Authorization") == "Bearer tok-1"
        finally:
            await client.close()
            await server.close()

    asyncio.run(go())


def test_runner_client_omits_authorization_without_token() -> None:
    async def go() -> None:
        for token in (None, ""):
            runner = _HeaderRecordingRunner()
            server = TestServer(runner.app)
            await server.start_server()
            base_url = f"http://127.0.0.1:{server.port}"
            client = RunnerClient(total_timeout_s=30.0)
            try:
                turn = await client.start_turn(base_url, _event(), token=token)
                await _drain(turn)
                await client.steer(base_url, _event(), token=token)
                await client.interrupt(base_url, "stop", token=token)

                assert "Authorization" not in runner.headers["event"]
                assert "Authorization" not in runner.headers["steer"]
                assert "Authorization" not in runner.headers["interrupt"]
            finally:
                await client.close()
                await server.close()

    asyncio.run(go())


# --- The interrupt RPC gets its own bound, separate from the streaming ---------
# budget (#742, a follow-up to #739 which bounded only one call site above this
# layer). Against a REAL local server whose /v1/interrupt accepts the
# connection and then answers nothing -- the wedged-runner shape -- so the
# assertion is on the actual client behavior, not a mock of it.


class _HangingInterruptRunner:
    """A runner whose ``/v1/interrupt`` accepts the connection and never
    answers, modelling the wedged runner #742 is about."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes([web.post("/v1/interrupt", self._interrupt)])
        self.hang = asyncio.Event()  # never set by the test: the handler never returns

    async def _interrupt(self, request: web.Request) -> web.Response:
        await self.hang.wait()
        return web.json_response({"ok": True})


def test_interrupt_is_bounded_by_its_own_timeout_not_the_streaming_budget() -> None:
    """The interrupt call must time out at RunnerClient's own
    ``interrupt_timeout_s``, not the session's streaming ``total_timeout_s`` --
    deliberately configured huge here so the test would hang for a long time
    (rather than pass by accident) if the interrupt call fell back to
    inheriting it."""

    async def go() -> None:
        runner = _HangingInterruptRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0, interrupt_timeout_s=0.2)
        try:
            loop = asyncio.get_event_loop()
            started = loop.time()
            with pytest.raises(TimeoutError):
                await client.interrupt(base_url, "stop")
            elapsed = loop.time() - started
            assert elapsed < 5.0  # nowhere near the 30s streaming budget
        finally:
            runner.hang.set()
            await client.close()
            await server.close()

    asyncio.run(go())


# --- W3C propagation + manually-owned streamed CLIENT spans (#1817) ----------


def _client_spans(span_recorder) -> list[ReadableSpan]:
    return span_recorder.spans(kind=SpanKind.CLIENT)


def _span_payload(span: ReadableSpan) -> str:
    return repr(
        (
            span.name,
            dict(span.attributes or {}),
            [
                (event.name, dict(event.attributes or {}))
                for event in span.events
            ],
            span.status.description,
        )
    )


def test_runner_http_injects_w3c_separately_from_auth_and_never_baggage(
    span_recorder,
) -> None:
    async def go() -> None:
        runner = _HeaderRecordingRunner()
        server = TestServer(runner.app)
        await server.start_server()
        port = server.port
        assert port is not None
        base_url = f"http://127.0.0.1:{port}"
        client = RunnerClient(total_timeout_s=30.0, tracer=span_recorder.tracer)
        trace_id = int("1234567890abcdef1234567890abcdef", 16)
        parent_span_id = int("1234567890abcdef", 16)
        remote = SpanContext(
            trace_id=trace_id,
            span_id=parent_span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState([("vendor", "opaque-example")]),
        )
        context = set_span_in_context(NonRecordingSpan(remote))
        baggage_value = "PRIVATE-BAGGAGE-MUST-NOT-CROSS"
        context = baggage.set_baggage("private-example", baggage_value, context=context)
        token = attach(context)
        auth = "auth-PLACEHOLDER-never-export"
        try:
            turn = await client.start_turn(base_url, _event(), token=auth)
            # A streamed CLIENT span belongs to TurnStream, not merely to the
            # response headers.  It cannot finish until the caller consumes or
            # explicitly abandons that owner.
            assert _client_spans(span_recorder) == []
            await _drain(turn)
            assert len(_client_spans(span_recorder)) == 1
            await client.steer(base_url, _event(), token=auth)
            await client.interrupt(base_url, "stop", token=auth)
        finally:
            detach(token)
            await client.close()
            await server.close()

        expected_prefix = f"00-{trace_id:032x}-"
        for route in ("event", "steer", "interrupt"):
            headers = runner.headers[route]
            assert headers["Authorization"] == f"Bearer {auth}"
            assert headers["traceparent"].startswith(expected_prefix)
            assert "tracestate" not in {name.lower() for name in headers}
            assert "baggage" not in {name.lower() for name in headers}

        spans = _client_spans(span_recorder)
        assert len(spans) == 3
        for span in spans:
            assert span.context is not None
            assert span.context.trace_id == trace_id
            assert span.parent is not None
            assert span.parent.span_id == parent_span_id
            assert span.status.status_code is StatusCode.OK
            assert span.attributes is not None
            assert span.attributes["http.request.method"] == "POST"
            assert span.attributes["server.address"] == "runner"
            assert span.attributes["server.port"] == port
            assert span.attributes["http.response.status_code"] == 200
            exported = _span_payload(span)
            assert auth not in exported
            assert baggage_value not in exported
            assert "/v1/" not in repr(dict(span.attributes))

    asyncio.run(go())


class _ResponseFailureRunner:
    def __init__(self, *, status: int, body: str) -> None:
        self.app = web.Application()
        self.app.add_routes([web.post("/v1/event", self._event)])
        self.status = status
        self.body = body

    async def _event(self, _request: web.Request) -> web.Response:
        return web.Response(status=self.status, text=self.body)


def test_non_200_ends_client_span_error_without_body_or_auth_leak(
    span_recorder,
) -> None:
    async def go() -> None:
        body = "PRIVATE-RUNNER-ERROR-BODY-MUST-NOT-BE-EXPORTED"
        auth = "auth-PLACEHOLDER-error-path"
        runner = _ResponseFailureRunner(status=503, body=body)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0, tracer=span_recorder.tracer)
        try:
            with pytest.raises(RunnerError, match=body):
                await client.start_turn(
                    f"http://127.0.0.1:{server.port}",
                    _event(),
                    token=auth,
                )
        finally:
            await client.close()
            await server.close()

        spans = _client_spans(span_recorder)
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["http.response.status_code"] == 503
        assert body not in _span_payload(span)
        assert auth not in _span_payload(span)

    asyncio.run(go())


class _MalformedStreamRunner:
    def __init__(self, raw: bytes) -> None:
        self.app = web.Application()
        self.app.add_routes([web.post("/v1/event", self._event)])
        self.raw = raw

    async def _event(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "application/x-ndjson"},
        )
        await response.prepare(request)
        await response.write(self.raw + b"\n")
        await response.write_eof()
        return response


def test_decode_failure_ends_stream_span_error_without_line_content(
    span_recorder,
) -> None:
    async def go() -> None:
        raw = b"PRIVATE-MALFORMED-NDJSON-MUST-NOT-BE-EXPORTED"
        runner = _MalformedStreamRunner(raw)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0, tracer=span_recorder.tracer)
        try:
            turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
            with pytest.raises(json.JSONDecodeError, match=r"line 1 column 1"):
                await _drain(turn)
        finally:
            await client.close()
            await server.close()

        spans = _client_spans(span_recorder)
        assert len(spans) == 1
        assert spans[0].status.status_code is StatusCode.ERROR
        assert raw.decode() not in _span_payload(spans[0])

    asyncio.run(go())


def test_explicit_abandonment_ends_stream_span_once(span_recorder) -> None:
    async def go() -> None:
        runner = _HeaderRecordingRunner()
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0, tracer=span_recorder.tracer)
        try:
            turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
            assert _client_spans(span_recorder) == []
            turn.close()
            turn.close()  # idempotent ownership: no duplicate end/export
        finally:
            await client.close()
            await server.close()

        assert len(_client_spans(span_recorder)) == 1

    asyncio.run(go())


class _HangingStreamRunner:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes([web.post("/v1/event", self._event)])
        self.release = asyncio.Event()

    async def _event(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "application/x-ndjson"},
        )
        await response.prepare(request)
        await response.write((TextDelta(text="safe").model_dump_json() + "\n").encode())
        await self.release.wait()
        await response.write_eof()
        return response


def test_stream_cancellation_ends_client_span_error(span_recorder) -> None:
    async def go() -> None:
        runner = _HangingStreamRunner()
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0, tracer=span_recorder.tracer)
        try:
            turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
            drain = asyncio.create_task(_drain(turn))
            await asyncio.sleep(0)
            drain.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain
        finally:
            runner.release.set()
            await client.close()
            await server.close()

        spans = _client_spans(span_recorder)
        assert len(spans) == 1
        assert spans[0].status.status_code is StatusCode.ERROR

    asyncio.run(go())
