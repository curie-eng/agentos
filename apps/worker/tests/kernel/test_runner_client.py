"""Regression guard for the RunnerClient turn-stream release contract that the
kernel's _consume relies on (verify-f1 coverage gap 1): the aiohttp response must
be released on every exit path -- normal completion and an exception mid-stream --
so a turn never leaks a connection. We spy on the response's release() because it
is what TurnStream.close (called from __aexit__) invokes."""

from __future__ import annotations

import asyncio
import inspect
import logging
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
from curie_worker.runner_client import RunnerClient, RunnerError, RunnerStreamTimeout

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


# --- Per-request timeout from the remaining delivery budget (ADR-0131, #1971) -
#
# ``runner_total_timeout_s`` stops being an independent clock and becomes a
# per-request CEILING inside the delivery's one overall deadline. Each
# budget-consuming RPC takes an optional ``remaining_s``; the effective timeout
# is ``min(runner_total_timeout_s, remaining_s)``, and ``remaining_s=None`` keeps
# the session default so every pre-existing caller is behaviourally unchanged.
#
# Asserted against a REAL local server that accepts the connection and then never
# answers -- the shape a budget must actually bound -- so these measure client
# behavior rather than a mock of it.


class _HangingEventRunner:
    """A runner whose ``/v1/event`` and ``/v1/interrupt`` accept the connection
    and never answer."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [
                web.post("/v1/event", self._hang),
                web.post("/v1/interrupt", self._hang),
            ]
        )
        self.hang = asyncio.Event()  # set only in teardown

    async def _hang(self, _request: web.Request) -> web.Response:
        await self.hang.wait()
        return web.json_response({"ok": True})


def test_start_turn_uses_the_remaining_budget_when_it_is_shorter() -> None:
    """A delivery with 0.3s of budget left must not hand the runner the full 30s
    session budget. Reverting the per-request override makes this call wait the
    whole streaming timeout, so the elapsed assertion is what goes red."""

    async def go() -> None:
        runner = _HangingEventRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        # 30s session default, deliberately huge relative to the budget below.
        client = RunnerClient(total_timeout_s=30.0)
        try:
            loop = asyncio.get_event_loop()
            started = loop.time()
            with pytest.raises(TimeoutError):
                await client.start_turn(base_url, _event(), remaining_s=0.3)
            assert loop.time() - started < 5.0
        finally:
            runner.hang.set()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_start_turn_without_a_remaining_budget_uses_the_session_default(
    caplog,
) -> None:
    """``remaining_s=None`` must leave the session timeout in charge: that is the
    path every leaseless caller and every existing test takes, and it must stay
    byte-identical in behavior."""

    async def go() -> None:
        runner = _HangingEventRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=0.3)
        try:
            loop = asyncio.get_event_loop()
            started = loop.time()
            with caplog.at_level(logging.INFO, logger="curie_worker.runner_client"):
                with pytest.raises(TimeoutError):
                    await client.start_turn(base_url, _event(), remaining_s=None)
            assert loop.time() - started < 5.0
            budget_records = [
                record
                for record in caplog.records
                if record.name == "curie_worker.runner_client"
                and hasattr(record, "effective_request_timeout_s")
            ]
            assert budget_records == [], caplog.text
            assert not any(
                "remaining" in record.getMessage()
                and "effective" in record.getMessage()
                for record in caplog.records
                if record.name == "curie_worker.runner_client"
            ), caplog.text
        finally:
            runner.hang.set()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_the_effective_timeout_is_the_min_of_the_budget_and_the_session_ceiling() -> None:
    """A remaining budget LARGER than the per-request ceiling must not raise the
    ceiling. Reverting ``min(...)`` to "the budget wins" would let a 30-minute
    delivery hand one runner request a 30-minute HTTP deadline."""

    async def go() -> None:
        runner = _HangingEventRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=0.3)
        try:
            loop = asyncio.get_event_loop()
            started = loop.time()
            with pytest.raises(TimeoutError):
                await client.start_turn(base_url, _event(), remaining_s=30.0)
            assert loop.time() - started < 5.0
        finally:
            runner.hang.set()
            await client.close()
            await server.close()

    asyncio.run(go())


def test_budgeted_request_logs_the_effective_timeout_bound(caplog) -> None:
    """A real request records the configured ceiling, the unmodified delivery
    remainder, and the effective post-floor timeout handed to aiohttp."""

    async def go() -> None:
        runner = _HeaderRecordingRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0)
        try:
            with caplog.at_level(logging.INFO, logger="curie_worker.runner_client"):
                turn = await client.start_turn(base_url, _event(), remaining_s=5.0)
                await _drain(turn)

            budget_records = [
                record
                for record in caplog.records
                if record.name == "curie_worker.runner_client"
                and hasattr(record, "effective_request_timeout_s")
            ]
            assert len(budget_records) == 1, caplog.text
            record = budget_records[0]
            assert record.levelno == logging.INFO
            assert getattr(record, "configured_runner_ceiling_s", None) == 30.0
            assert getattr(record, "remaining_delivery_s", None) == 5.0
            assert getattr(record, "effective_request_timeout_s", None) == 5.0
            message = record.getMessage()
            normalized_message = message.lower()
            assert "configured" in normalized_message
            assert "ceiling" in normalized_message
            assert "30.0" in message
            assert "remaining" in normalized_message
            assert "effective" in normalized_message
            assert message.count("5.0") >= 2
        finally:
            await client.close()
            await server.close()

    asyncio.run(go())


def test_budgeted_status_does_not_log_the_turn_timeout_bound(caplog) -> None:
    """Budget propagation still bounds control RPCs, but the effective turn
    timeout record belongs only to the request that opens the streamed turn.
    Polling status must not emit that operator-facing message or its structured
    fields.
    """

    async def go() -> None:
        app = web.Application()

        async def status_handler(_request: web.Request) -> web.Response:
            return web.json_response({"turn_active": False})

        app.add_routes([web.get("/status", status_handler)])
        server = TestServer(app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0)
        try:
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="curie_worker.runner_client"):
                status = await client.status(base_url, remaining_s=5.0)

            assert status["turn_active"] is False
            records = [
                record
                for record in caplog.records
                if record.name == "curie_worker.runner_client"
            ]
            assert not any(
                record.levelno == logging.INFO
                and "runner request timeout bound" in record.getMessage()
                for record in records
            ), caplog.text
            assert not any(
                hasattr(record, "effective_request_timeout_s")
                for record in records
            ), caplog.text
        finally:
            await client.close()
            await server.close()

    asyncio.run(go())


def test_a_remaining_budget_does_not_break_a_responsive_turn() -> None:
    """The positive control for the three timeout tests above: with a budget in
    hand and a runner that answers, the turn still opens and streams. Without it
    they would all pass against a client whose every request now fails."""

    async def go() -> None:
        runner = _HeaderRecordingRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0)
        try:
            turn = await client.start_turn(base_url, _event(), remaining_s=5.0)
            await _drain(turn)
            assert "event" in runner.headers
            assert await client.steer(base_url, _event(), remaining_s=5.0) is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(go())


def test_interrupt_takes_no_remaining_budget_while_the_other_rpcs_do() -> None:
    """A structural guard against a future "simplification" that folds interrupt
    into the budget path. ``/v1/interrupt`` is the fail-closed path a lost lease
    fires: deriving its timeout from a budget that may already be exhausted would
    make the fence unable to stop the runner it just fenced."""
    for name in ("start_turn", "steer", "status", "snapshot", "reset"):
        parameters = inspect.signature(getattr(RunnerClient, name)).parameters
        assert "remaining_s" in parameters, f"{name} must accept a remaining budget"

    assert "remaining_s" not in inspect.signature(RunnerClient.interrupt).parameters, (
        "interrupt must never take a remaining budget: it is the fail-closed "
        "control path and keeps its own independent timeout"
    )


def test_interrupt_keeps_its_own_timeout_under_a_huge_streaming_budget() -> None:
    """The behavioral half of the guard above. With a 30s session budget and a
    wedged runner, the interrupt must still return at its own 0.2s bound."""

    async def go() -> None:
        runner = _HangingEventRunner()
        server = TestServer(runner.app)
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        client = RunnerClient(total_timeout_s=30.0, interrupt_timeout_s=0.2)
        try:
            loop = asyncio.get_event_loop()
            started = loop.time()
            with pytest.raises(TimeoutError):
                await client.interrupt(base_url, "delivery lease lost")
            assert loop.time() - started < 5.0
        finally:
            runner.hang.set()
            await client.close()
            await server.close()

    asyncio.run(go())


# --- The streaming boundary owns its own timeout terminal record (#2011) ------
# ``start_turn``'s ``_rpc`` span has already closed by the time the NDJSON body
# is streamed, so an expiring total/sock_read budget used to leave NO record at
# this boundary at all, and handed the kernel a bare ``TimeoutError`` whose
# ``str()`` is the empty string.


def test_stream_timeout_raises_a_named_timeout_and_logs_the_expired_budget(
    make_harness, caplog
) -> None:
    """#2011: iterating a turn whose runner hangs past the client's budget must
    raise a ``RunnerStreamTimeout`` -- still a ``TimeoutError``, so every
    existing ``except TimeoutError`` keeps catching it -- whose message names the
    normalized exception class and the budget that expired, and must emit a
    correlated WARNING on the client's own logger. Today the raised exception is
    a bare ``TimeoutError`` that stringifies to "" and nothing is logged here."""

    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()  # never set: the response hangs after a prefix
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="x")]
            handle = await asyncio.to_thread(h.substrate.claim, "tStreamTimeout")
            client = RunnerClient(total_timeout_s=5.0)
            try:
                with caplog.at_level(logging.WARNING, logger="curie_worker.runner_client"):
                    turn = await client.start_turn(
                        handle.base_url, _event(), remaining_s=0.2
                    )
                    with pytest.raises(TimeoutError) as excinfo:
                        async with turn:
                            async for _frame in turn:
                                pass

                exc = excinfo.value
                assert isinstance(exc, RunnerStreamTimeout)
                assert isinstance(exc, TimeoutError)  # existing handlers still catch it
                assert str(exc).strip(), "a stream timeout must not stringify to nothing"
                assert "Timeout" in str(exc)  # the normalized underlying class
                # The delivery had only 0.2s left, so that effective request
                # bound -- not the configured 5s ceiling -- is what expired.
                assert "0.2" in str(exc)
                assert "5.0s" not in str(exc)

                warnings = [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == "curie_worker.runner_client"
                    and record.levelno >= logging.WARNING
                ]
                assert warnings, caplog.text
                assert any(
                    "Timeout" in message and "0.2" in message for message in warnings
                ), warnings
                assert all("5.0s" not in message for message in warnings)
            finally:
                hold.set()
                await client.close()

    asyncio.run(go())
