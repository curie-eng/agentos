"""Regression guard for the RunnerClient turn-stream release contract that the
kernel's _consume relies on (verify-f1 coverage gap 1): the aiohttp response must
be released on every exit path -- normal completion and an exception mid-stream --
so a turn never leaks a connection. We spy on the response's release() because it
is what TurnStream.close (called from __aexit__) invokes."""

from __future__ import annotations

import asyncio
import tracemalloc
from typing import Any

import pytest
from aci_protocol import Event, Final, SessionStatus, TextDelta
from aiohttp import web
from aiohttp.test_utils import TestServer
from curie_runner import RunTracer, SideEffectClassifier, create_app
from curie_runner import server as runner_server
from curie_runner.fake import FakeModelSession
from curie_runner.session import SessionRunner
from curie_worker.runner_client import RunnerClient, RunnerError

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
        base_url = f"http://127.0.0.1:{server.port}"
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


def test_snapshot_refuses_an_oversized_body_before_json_decoding() -> None:
    async def go() -> None:
        app = web.Application()

        async def oversized(_request: web.Request) -> web.Response:
            return web.Response(
                body=b'{"patch_base64":"' + (b"A" * 140_000) + b'"}',
                content_type="application/json",
            )

        app.add_routes([web.post("/v1/snapshot", oversized)])
        server = TestServer(app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0, snapshot_patch_max_bytes=16)
        try:
            with pytest.raises(RunnerError, match="invalid bounded payload"):
                await client.snapshot(
                    f"http://127.0.0.1:{server.port}", token="runner-token"
                )
        finally:
            await client.close()
            await server.close()

    asyncio.run(go())


# --- Successful Final must not RST the runner before write_eof (issue #1958) --
# Kernel._consume stops applying frames at Final, then TurnStream.__aexit__
# releases the aiohttp response. Against the real runner HTTP stream that
# races server._event's write_eof (after aclosing teardown) and logs
# ClientConnectionResetError on a completed turn. Drain the rest of the body
# before release so write_eof still sees an open transport.


def test_kernel_final_break_closes_real_runner_stream_without_write_eof_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_errors: list[BaseException] = []
    write_eof_errors: list[BaseException] = []
    real_write_eof = web.StreamResponse.write_eof
    original_event = runner_server._event

    async def spy_write_eof(self: web.StreamResponse, data: bytes = b"") -> None:
        try:
            await real_write_eof(self, data)
        except BaseException as exc:
            write_eof_errors.append(exc)
            raise

    monkeypatch.setattr(web.StreamResponse, "write_eof", spy_write_eof)

    async def go() -> None:
        handler_done = asyncio.Event()

        async def wrapped_event(request: web.Request) -> web.StreamResponse:
            try:
                return await original_event(request)
            except BaseException as exc:
                handler_errors.append(exc)
                raise
            finally:
                handler_done.set()

        monkeypatch.setattr(runner_server, "_event", wrapped_event)
        fake = FakeModelSession()
        runner = SessionRunner(
            session_factory=lambda: fake,
            ceiling=0,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(),
            trace_name="t",
        )
        original_record = runner._record_turn

        async def delayed_record(*args: Any, **kwargs: Any) -> Any:
            # Production posts the transcript after yielding Final and before
            # the generator ends. That is the window _consume uses to break
            # and release, which then cancels _event before write_eof.
            await asyncio.sleep(0.05)
            return await original_record(*args, **kwargs)

        runner._record_turn = delayed_record  # type: ignore[method-assign]
        await runner.start()
        server = TestServer(create_app(runner))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0)
        saw_final = False
        try:
            turn = await client.start_turn(base_url, _event())
            release_calls = _spy_release(turn)
            async with turn:
                async for frame in turn:
                    if isinstance(frame, Final):
                        assert frame.status == DONE
                        saw_final = True
                        break
            await asyncio.wait_for(handler_done.wait(), timeout=5.0)
            assert saw_final
            assert handler_errors == [], (
                f"successful Final produced a runner handler error: {handler_errors!r}"
            )
            assert write_eof_errors == [], (
                "successful Final closed the runner transport before write_eof: "
                f"{write_eof_errors!r}"
            )
            assert release_calls["n"] >= 1
        finally:
            await client.close()
            await server.close()
            await runner.close()

    asyncio.run(go())


class _PostFinalRunner:
    """Real HTTP peer with controllable behavior after a valid Final."""

    def __init__(self, *, stall: bool = False, tail_bytes: int = 0) -> None:
        self.app = web.Application()
        self.app.add_routes([web.post("/v1/event", self._event)])
        self.stall = stall
        self.tail_bytes = tail_bytes
        self.after_final = asyncio.Event()
        self.unblock = asyncio.Event()
        self.handler_done = asyncio.Event()
        self.handler_errors: list[BaseException] = []

    async def _event(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "application/x-ndjson"}
        )
        await response.prepare(request)
        try:
            await response.write(
                (Final(text="done", status=DONE).model_dump_json() + "\n").encode()
            )
            self.after_final.set()
            if self.stall:
                await self.unblock.wait()
            chunk = b"x" * (64 * 1024)
            remaining = self.tail_bytes
            while remaining:
                size = min(remaining, len(chunk))
                await response.write(chunk[:size])
                remaining -= size
            await response.write_eof()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            # Expected only when a timeout/cancellation test deliberately
            # releases the client response before unblocking this handler.
            self.handler_errors.append(exc)
        finally:
            self.handler_done.set()
        return response


async def _break_after_final(turn: Any, final_seen: asyncio.Event | None = None) -> None:
    async with turn:
        async for frame in turn:
            if isinstance(frame, Final):
                if final_seen is not None:
                    final_seen.set()
                break


def test_post_final_stall_has_a_short_cleanup_bound_and_releases_response() -> None:
    async def go() -> None:
        runner = _PostFinalRunner(stall=True)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0)
        turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
        release_calls = _spy_release(turn)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(_break_after_final(turn), timeout=2.0)
            assert loop.time() - started < 2.0
            assert release_calls["n"] >= 1
        finally:
            runner.unblock.set()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_cancellation_during_post_final_drain_propagates_and_releases_response() -> None:
    async def go() -> None:
        runner = _PostFinalRunner(stall=True)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0)
        turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
        release_calls = _spy_release(turn)
        final_seen = asyncio.Event()
        task = asyncio.create_task(_break_after_final(turn, final_seen))
        try:
            await asyncio.wait_for(final_seen.wait(), timeout=1.0)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert release_calls["n"] >= 1
        finally:
            runner.unblock.set()
            if not task.done():
                task.cancel()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_unexpected_post_final_cleanup_error_is_ignored_and_releases_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        runner = _PostFinalRunner(stall=True)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0)
        turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
        release_calls = _spy_release(turn)

        async def cleanup_boom(_reader: Any, _size: int) -> bytes:
            raise RuntimeError("unexpected cleanup failure")

        try:
            monkeypatch.setattr(type(turn._response.content), "read", cleanup_boom)
            await _break_after_final(turn)
            assert release_calls["n"] >= 1
        finally:
            runner.unblock.set()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_large_post_final_tail_is_discarded_without_aggregation() -> None:
    async def go() -> None:
        runner = _PostFinalRunner(tail_bytes=32 * 1024 * 1024)
        server = TestServer(runner.app)
        await server.start_server()
        client = RunnerClient(total_timeout_s=30.0)
        turn = await client.start_turn(f"http://127.0.0.1:{server.port}", _event())
        release_calls = _spy_release(turn)
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            await asyncio.wait_for(_break_after_final(turn), timeout=5.0)
            _current, peak = tracemalloc.get_traced_memory()
            assert peak - baseline < 8 * 1024 * 1024
            assert release_calls["n"] >= 1
            await asyncio.wait_for(runner.handler_done.wait(), timeout=1.0)
            assert runner.handler_errors == []
        finally:
            tracemalloc.stop()
            await client.close()
            await server.close()

    asyncio.run(go())
