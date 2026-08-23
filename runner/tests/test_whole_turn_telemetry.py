"""Runtime proofs for the runner's part of the whole-turn telemetry chain.

These tests deliberately exercise the OTLP HTTP boundary.  Looking at provider
configuration would not prove that the incoming HTTP parent survived until
``agent.run``, that logs were correlated, or that the batch was flushed before
the worker is allowed to tear the sandbox down.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest
from aci_protocol import SessionStatus, parse_ndjson
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from curie_runner import RunTracer, SideEffectClassifier, create_app
from curie_runner.fake import FakeModelSession
from curie_runner.session import SessionRunner
from curie_telemetry import TelemetryRuntime, configure, emit_log_event
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Status

_TRACE_ID = "11111111111111111111111111111111"
_PARENT_ID = "2222222222222222"
_PRIVATE_MARKER = "sk-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
_EVENT = {"kind": "event", "type": "message", "text": "hello", "user": "U", "ts": "1"}
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _OtlpCapture:
    server: ThreadingHTTPServer
    traces: list[ExportTraceServiceRequest] = field(default_factory=list)
    logs: list[ExportLogsServiceRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def append(self, path: str, body: bytes) -> None:
        with self.lock:
            if path == "/v1/traces":
                request = ExportTraceServiceRequest()
                request.ParseFromString(body)
                self.traces.append(request)
                return
            if path == "/v1/logs":
                request = ExportLogsServiceRequest()
                request.ParseFromString(body)
                self.logs.append(request)
                return
            raise AssertionError(f"unexpected OTLP path: {path}")


def _handler(capture_ref: list[_OtlpCapture]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            capture_ref[0].append(self.path, body)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@pytest.fixture
def otlp_capture() -> Iterator[_OtlpCapture]:
    ref: list[_OtlpCapture] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ref))
    server.daemon_threads = True
    capture = _OtlpCapture(server)
    ref.append(capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def clean_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)


def _spans(capture: _OtlpCapture) -> list[Any]:
    return [
        span
        for request in capture.traces
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


def _logs(capture: _OtlpCapture) -> list[Any]:
    return [
        record
        for request in capture.logs
        for resource_logs in request.resource_logs
        for scope_logs in resource_logs.scope_logs
        for record in scope_logs.log_records
    ]


def _body(record: Any) -> str:
    return record.body.string_value


def _value(value: Any) -> object:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "int_value":
        return value.int_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "double_value":
        return value.double_value
    if kind == "array_value":
        return tuple(_value(item) for item in value.array_value.values)
    return None


def _attributes(record: Any) -> dict[str, object]:
    return {attribute.key: _value(attribute.value) for attribute in record.attributes}


def _runtime(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> TelemetryRuntime:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    return configure(service_name="curie-runner", service_version="test")


def _runner(
    runtime: TelemetryRuntime,
    session_factory: Any = FakeModelSession,
) -> SessionRunner:
    return SessionRunner(
        session_factory=session_factory,
        ceiling=0,
        tracer=RunTracer(runtime.tracer),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:agent-agent-example-thread-thread-example",
        session_id="agent-agent-example-thread-thread-example",
        model="fake-model",
        telemetry=runtime,
    )


def _turn(runner: SessionRunner, headers: dict[str, str] | None = None) -> list[Any]:
    async def go() -> list[Any]:
        await runner.start()
        async with TestClient(TestServer(create_app(runner))) as client:
            response = await client.post("/v1/event", json=_EVENT, headers=headers)
            assert response.status == 200
            return parse_ndjson(await response.text())

    return anyio.run(go)


def test_http_traceparent_is_the_direct_parent_of_agent_run_and_flushes_before_reply(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
) -> None:
    runtime = _runtime(monkeypatch, otlp_capture.endpoint)
    events = _turn(
        _runner(runtime),
        {
            "traceparent": f"00-{_TRACE_ID}-{_PARENT_ID}-01",
            "tracestate": "vendor=OPAQUE_TRACE_STATE_SENTINEL",
        },
    )

    assert events[-1].status is SessionStatus.DONE
    agent = next(span for span in _spans(otlp_capture) if span.name == "agent.run")
    assert agent.trace_id.hex() == _TRACE_ID
    assert agent.parent_span_id.hex() == _PARENT_ID
    assert agent.status.code == Status.STATUS_CODE_OK
    assert agent.trace_state == ""
    assert "OPAQUE_TRACE_STATE_SENTINEL" not in repr(otlp_capture.traces)
    assert any(
        record.trace_id == agent.trace_id
        and record.span_id == agent.span_id
        and _body(record) == "agent.run.completed"
        for record in _logs(otlp_capture)
    ), "the terminal runner log must be exported before the HTTP EOF is observable"


@pytest.mark.parametrize("traceparent", [None, "00-not-a-valid-parent"])
def test_missing_or_malformed_http_parent_is_a_safe_new_root(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
    caplog: pytest.LogCaptureFixture,
    traceparent: str | None,
) -> None:
    runtime = _runtime(monkeypatch, otlp_capture.endpoint)
    headers = {} if traceparent is None else {"traceparent": traceparent}
    with caplog.at_level(logging.WARNING):
        events = _turn(_runner(runtime), headers)

    assert events[-1].status is SessionStatus.DONE
    agent = next(span for span in _spans(otlp_capture) if span.name == "agent.run")
    assert agent.parent_span_id == b""
    rendered = " ".join(record.getMessage() for record in caplog.records)
    if traceparent is None:
        assert "ignored malformed trace context" not in rendered
    else:
        assert "ignored malformed trace context" in rendered
        assert traceparent not in rendered


_ARBITRARY_EXCEPTION = (
    "review the quarterly plan; model output was incomplete; "
    "tool exception calendar lookup failed"
)


class _ExplodingSession(FakeModelSession):
    async def receive_turn(self) -> Any:
        if False:  # make this an async generator while failing before its first item
            yield None
        raise RuntimeError(_ARBITRARY_EXCEPTION)


def test_caught_failure_marks_agent_run_error_and_exports_a_redacted_correlated_log(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
) -> None:
    runtime = _runtime(monkeypatch, otlp_capture.endpoint)
    events = _turn(_runner(runtime, _ExplodingSession))

    assert events[-1].status is SessionStatus.CLASSIFIED_FAILURE
    agent = next(span for span in _spans(otlp_capture) if span.name == "agent.run")
    assert agent.status.code == Status.STATUS_CODE_ERROR
    correlated = [
        record
        for record in _logs(otlp_capture)
        if record.trace_id == agent.trace_id and record.span_id == agent.span_id
    ]
    assert [_body(record) for record in correlated] == ["agent.run.failed"]
    assert all(record.severity_text == "ERROR" for record in correlated)
    exported = repr(otlp_capture.traces) + repr(otlp_capture.logs)
    assert _PRIVATE_MARKER not in exported
    assert _ARBITRARY_EXCEPTION not in exported


def test_arbitrary_prompt_model_and_tool_exception_stays_in_stderr_only(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime(monkeypatch, otlp_capture.endpoint)
    with caplog.at_level(logging.ERROR, logger="curie_runner.session"):
        events = _turn(_runner(runtime, _ExplodingSession))

    assert events[-1].status is SessionStatus.CLASSIFIED_FAILURE
    assert any(_ARBITRARY_EXCEPTION in record.getMessage() for record in caplog.records)
    assert _ARBITRARY_EXCEPTION not in repr(otlp_capture.logs)
    assert [_body(record) for record in _logs(otlp_capture)] == ["agent.run.failed"]


def test_runner_export_boundary_is_recursively_redacted_and_service_closed(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
) -> None:
    runtime = _runtime(monkeypatch, otlp_capture.endpoint)
    logger = logging.getLogger("curie-runner.telemetry-boundary-test")
    runtime.attach_logging(logger)

    with runtime.tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("curie.session_id", "safe-session")
        span.set_attribute("messaging.system", "otherwise-valid-for-another-service")
        span.set_attribute("unknown.attribute", "must-not-export")
        span.set_attribute("langfuse.trace.name", ["safe", _PRIVATE_MARKER])
        logger.error(
            "runner failed authorization=%s",
            _PRIVATE_MARKER,
            extra={
                "curie.session_id": "safe-session",
                "messaging.system": "cross-service",
                "unknown.attribute": _PRIVATE_MARKER,
            },
        )
        emit_log_event(logger, "agent.run.failed", level=logging.ERROR)
    assert runtime.force_flush(timeout_millis=500)

    span_record = next(span for span in _spans(otlp_capture) if span.name == "agent.run")
    span_attributes = _attributes(span_record)
    assert span_attributes["curie.session_id"] == "safe-session"
    assert "messaging.system" not in span_attributes
    assert "unknown.attribute" not in span_attributes
    assert "langfuse.trace.name" not in span_attributes
    log_record = next(
        record for record in _logs(otlp_capture) if _body(record) == "agent.run.failed"
    )
    log_attributes = _attributes(log_record)
    assert log_attributes == {}, "curated log events carry correlation only, no values"
    assert _PRIVATE_MARKER not in repr(otlp_capture.traces)
    assert _PRIVATE_MARKER not in repr(otlp_capture.logs)
    runtime.shutdown(timeout_millis=2_000)


class _OrderingRuntime:
    def __init__(self, calls: list[str], *, fail_flush: bool = False) -> None:
        from opentelemetry import trace

        self.tracer = trace.get_tracer("runner-ordering-test")
        self.calls = calls
        self.fail_flush = fail_flush

    def force_flush(self, *, timeout_millis: int) -> bool:
        self.calls.append(f"flush:{timeout_millis}")
        if self.fail_flush:
            raise RuntimeError(f"flush rejected authorization={_PRIVATE_MARKER}")
        return True

    def shutdown(self, *, timeout_millis: int = 2_000) -> bool:
        self.calls.append(f"shutdown:{timeout_millis}")
        return True


def test_terminal_batch_flush_is_bounded_and_precedes_http_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    runtime = _OrderingRuntime(calls)
    original = web.StreamResponse.write_eof

    async def record_eof(self: web.StreamResponse, data: bytes = b"") -> None:
        calls.append("eof")
        await original(self, data)

    monkeypatch.setattr(web.StreamResponse, "write_eof", record_eof)
    events = _turn(_runner(runtime))  # type: ignore[arg-type]

    assert events[-1].status is SessionStatus.DONE
    flush_index = next(index for index, call in enumerate(calls) if call.startswith("flush:"))
    eof_index = calls.index("eof")
    assert flush_index < eof_index
    assert int(calls[flush_index].partition(":")[2]) <= 500


def test_flush_failure_is_redacted_and_does_not_reclassify_the_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _OrderingRuntime([], fail_flush=True)
    with caplog.at_level(logging.WARNING):
        events = _turn(_runner(runtime))  # type: ignore[arg-type]

    assert events[-1].status is SessionStatus.DONE
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "telemetry flush failed" in rendered
    assert _PRIVATE_MARKER not in rendered


def test_unreachable_exporter_cannot_hold_runner_cleanup_past_the_shutdown_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{probe.getsockname()[1]}"
    runtime = _runtime(monkeypatch, endpoint)

    started = time.monotonic()
    events = _turn(_runner(runtime))
    elapsed = time.monotonic() - started

    assert events[-1].status is SessionStatus.DONE
    assert elapsed < 3.0, "500ms pre-EOF flush plus 2s shutdown must remain bounded"


def test_no_endpoint_keeps_diagnostics_but_installs_no_retrying_export_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = configure(service_name="curie-runner", service_version="test")

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        events = _turn(_runner(runtime))
    elapsed = time.monotonic() - started

    assert events[-1].status is SessionStatus.DONE
    assert any("turn end" in record.getMessage() for record in caplog.records)
    assert not any("export" in record.getMessage().lower() for record in caplog.records)
    assert elapsed < 1.0, "an unconfigured exporter must not add retry or shutdown delay"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_immediate_post_turn_process_termination_still_exports_agent_run(
    monkeypatch: pytest.MonkeyPatch,
    otlp_capture: _OtlpCapture,
) -> None:
    """The worker may delete a runner as soon as EOF arrives; the span must predate EOF."""

    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "CURIE_PLUGIN_DIR": str(_REPO_ROOT / "examples/weather"),
            "CURIE_SESSION_ID": "agent-agent-example-thread-thread-termination",
            "CURIE_SANDBOX_ID": "sandbox-example",
            "CURIE_BUDGET": json.dumps(
                {"max_output_tokens_per_run": 1000, "max_usd_per_day": 5.0}
            ),
            "CURIE_FAKE_MODEL": "1",
            "CURIE_RUNNER_PORT": str(port),
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_capture.endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(_REPO_ROOT / "runner/src"),
                    str(_REPO_ROOT / "packages/aci-protocol/src"),
                    str(_REPO_ROOT / "packages/plugin-format/src"),
                    str(_REPO_ROOT / "packages/telemetry/src"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        }
    )
    process = subprocess.Popen(  # noqa: S603 - fixed module in this checkout
        [sys.executable, "-m", "curie_runner"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(  # noqa: S310 - loopback test server
                    f"http://127.0.0.1:{port}/healthz", timeout=0.2
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    output = process.communicate(timeout=1)[0]
                    raise AssertionError(f"runner did not become healthy:\n{output}") from None
                time.sleep(0.05)

        request = urllib.request.Request(  # noqa: S310 - loopback test server
            f"http://127.0.0.1:{port}/v1/event",
            data=json.dumps(_EVENT).encode(),
            headers={
                "content-type": "application/json",
                "traceparent": f"00-{_TRACE_ID}-{_PARENT_ID}-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            assert parse_ndjson(response.read().decode())[-1].status is SessionStatus.DONE
        process.terminate()
        output = process.communicate(timeout=3)[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    agent = next(span for span in _spans(otlp_capture) if span.name == "agent.run")
    assert agent.trace_id.hex() == _TRACE_ID
    assert agent.parent_span_id.hex() == _PARENT_ID
    assert _PRIVATE_MARKER not in output
