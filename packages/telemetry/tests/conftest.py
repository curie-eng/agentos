"""Local OTLP HTTP capture used by the shared telemetry contract tests."""

from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)


@dataclass
class OtlpHttpCapture:
    """Decoded OTLP requests received by one loopback HTTP server."""

    server: ThreadingHTTPServer
    traces: list[ExportTraceServiceRequest] = field(default_factory=list)
    logs: list[ExportLogsServiceRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def append(self, path: str, body: bytes) -> None:
        with self._lock:
            if path == "/v1/traces":
                request = ExportTraceServiceRequest()
                request.ParseFromString(body)
                self.traces.append(request)
            elif path == "/v1/logs":
                request = ExportLogsServiceRequest()
                request.ParseFromString(body)
                self.logs.append(request)
            else:
                raise AssertionError(f"unexpected OTLP path {path!r}")


def _handler(capture_ref: list[OtlpHttpCapture]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            if self.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            capture_ref[0].append(self.path, body)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@pytest.fixture
def otlp_http_capture() -> Iterator[OtlpHttpCapture]:
    capture_ref: list[OtlpHttpCapture] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(capture_ref))
    server.daemon_threads = True
    capture = OtlpHttpCapture(server)
    capture_ref.append(capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


_OTEL_ENV = (
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_COMPRESSION",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_LOGS_COMPRESSION",
)


@pytest.fixture(autouse=True)
def clean_otel_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
