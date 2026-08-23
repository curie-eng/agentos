"""API tracing and the API-owned Valkey producer boundary."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
from aci_protocol import QueuedTurn
from curie_api.config import get_settings
from curie_api.delivery import enqueue_owned
from curie_api.main import create_app
from curie_telemetry.carrier import TRACE_CONTEXT_FIELD
from curie_test_support.valkey import (
    VALKEY_HOST as _VALKEY_HOST,
)
from curie_test_support.valkey import (
    VALKEY_PORT as _VALKEY_PORT,
)
from curie_test_support.valkey import (
    VALKEY_PW as _VALKEY_PW,
)
from curie_test_support.valkey import (
    connect_or_skip,
)
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

TRACE_ID = "11111111111111111111111111111111"
PARENT_ID = "2222222222222222"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"
TURN_TEXT = "PLACEHOLDER_TURN_TEXT"


@dataclass
class _OtlpCapture:
    server: ThreadingHTTPServer
    traces: list[ExportTraceServiceRequest] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


def _handler(capture_ref: list[_OtlpCapture]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            if self.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            if self.path == "/v1/traces":
                request = ExportTraceServiceRequest()
                request.ParseFromString(body)
                capture_ref[0].traces.append(request)
            assert self.path in {"/v1/traces", "/v1/logs"}
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@pytest.fixture
def otlp_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_OtlpCapture]:
    capture_ref: list[_OtlpCapture] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(capture_ref))
    server.daemon_threads = True
    capture = _OtlpCapture(server)
    capture_ref.append(capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", capture.endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    try:
        yield capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def runs_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    stream = f"test:curie:runs:otel:{uuid.uuid4().hex}"
    monkeypatch.setenv("RUNS_STREAM", stream)
    get_settings.cache_clear()
    try:
        yield stream
    finally:
        get_settings.cache_clear()


@pytest.fixture
def valkey(runs_stream: str) -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
    try:
        yield client
    finally:
        client.delete(runs_stream)
        for key in client.scan_iter(match="test:curie:otel:*"):
            client.delete(key)
        client.close()


def _any_value(value: Any) -> object:
    selected = value.WhichOneof("value")
    if selected == "string_value":
        return value.string_value
    if selected == "bool_value":
        return value.bool_value
    if selected == "int_value":
        return value.int_value
    return None


def _attrs(span: Any) -> dict[str, object]:
    return {item.key: _any_value(item.value) for item in span.attributes}


def _spans(capture: _OtlpCapture) -> list[Any]:
    return [
        span
        for request in capture.traces
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


def test_http_middleware_adopts_valid_context_and_ignores_bad_context_value_free(
    otlp_capture: _OtlpCapture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed = "MALFORMED_TRACE_CONTEXT_SENTINEL"
    with caplog.at_level(logging.WARNING, logger="curie_api"):
        with TestClient(create_app()) as client:
            inherited = client.get(
                "/health?query=PLACEHOLDER_QUERY",
                headers={
                    "traceparent": TRACEPARENT,
                    "authorization": "PLACEHOLDER_AUTH",
                },
            )
            missing = client.get("/health")
            bad = client.get("/health", headers={"traceparent": malformed})

    assert inherited.status_code == missing.status_code == bad.status_code == 200
    health_spans = [span for span in _spans(otlp_capture) if span.name == "GET /health"]
    assert len(health_spans) == 3
    inherited_span = next(
        span for span in health_spans if span.trace_id == bytes.fromhex(TRACE_ID)
    )
    assert inherited_span.parent_span_id == bytes.fromhex(PARENT_ID)
    rooted = [span for span in health_spans if span is not inherited_span]
    assert all(not span.parent_span_id for span in rooted)

    allowed = {
        "http.request.method",
        "server.address",
        "server.port",
        "http.response.status_code",
    }
    assert all(set(_attrs(span)) <= allowed for span in health_spans)
    exported = repr(health_spans)
    assert "PLACEHOLDER_QUERY" not in exported
    assert "PLACEHOLDER_AUTH" not in exported
    assert malformed not in exported
    warnings = " ".join(record.getMessage() for record in caplog.records)
    assert warnings.count("ignored malformed trace context") == 1
    assert malformed not in warnings


def test_channel_turn_writes_one_payload_plus_carrier_and_keeps_langfuse_identity(
    _disposable_db: Any,
    clean_db: None,
    auth_headers: dict[str, str],
    otlp_capture: _OtlpCapture,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    with TestClient(create_app()) as client:
        created = client.post(
            "/agents",
            headers=auth_headers,
            json={
                "name": "acme-otel-api",
                "channel": {
                    "kind": "email",
                    "address": "otel@example.test",
                    "endpoint": "http://adapter.example.test/reply",
                    "adapter": "agentmail-sandbox",
                },
            },
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        accepted = client.post(
            "/channels/turns",
            headers={**auth_headers, "traceparent": TRACEPARENT},
            json={
                "kind": "email",
                "address": "otel@example.test",
                "delivery_id": "delivery-otel-api",
                "conversation_id": "thread-otel-api",
                "author": "user@example.test",
                "text": TURN_TEXT,
                "reply_ref": "reply-otel-api",
            },
        )
        assert accepted.status_code == 200, accepted.text

    [(stream_id, fields)] = valkey.xrange(runs_stream)
    assert stream_id == accepted.json()["stream_id"]
    assert set(fields) == {"payload", TRACE_CONTEXT_FIELD}
    turn = QueuedTurn.model_validate_json(fields["payload"])
    assert turn.text == TURN_TEXT
    assert TRACE_CONTEXT_FIELD not in json.loads(fields["payload"])

    spans = _spans(otlp_capture)
    producer = next(span for span in spans if span.name == "send curie:runs")
    server = next(span for span in spans if span.span_id == producer.parent_span_id)
    assert server.trace_id == producer.trace_id == bytes.fromhex(TRACE_ID)
    assert server.parent_span_id == bytes.fromhex(PARENT_ID)
    producer_attrs = _attrs(producer)
    assert producer_attrs["messaging.system"] == "valkey"
    assert producer_attrs["messaging.destination.name"] == runs_stream
    assert producer_attrs["messaging.operation.type"] == "send"

    session_id = f"agent-{agent_id}-thread-{turn.conversation_id}"
    server_attrs = _attrs(server)
    assert server_attrs["langfuse.trace.name"] == f"curie-run:{session_id}"
    assert server_attrs["langfuse.session.id"] == session_id
    assert server_attrs["langfuse.user.id"] == turn.author

    carrier = json.loads(fields[TRACE_CONTEXT_FIELD])
    assert carrier["traceparent"].startswith(
        f"00-{TRACE_ID}-{producer.span_id.hex()}-"
    )
    exported = repr(spans)
    assert TURN_TEXT not in exported
    assert "reply-otel-api" not in exported
    assert "authorization" not in exported.lower()


def test_owner_checked_enqueue_without_a_carrier_remains_payload_only(
    valkey: redis.Redis, runs_stream: str
) -> None:
    async def exercise() -> dict[str, str]:
        client = aioredis.Redis(
            host=_VALKEY_HOST,
            port=_VALKEY_PORT,
            password=_VALKEY_PW or None,
            decode_responses=True,
        )
        key = f"test:curie:otel:{uuid.uuid4().hex}"
        owner = "pending:legacy-owner"
        try:
            await client.set(key, owner, ex=60)
            enqueued, _stream_id = await enqueue_owned(
                client,
                key=key,
                stream=runs_stream,
                owner=owner,
                payload='{"legacy":"payload"}',
                payload_field="payload",
                lease_s=60,
            )
            assert enqueued
            [(_entry_id, fields)] = await client.xrange(runs_stream)
            return fields
        finally:
            await client.delete(key)
            await client.aclose()

    fields = asyncio.run(exercise())
    assert fields == {"payload": '{"legacy":"payload"}'}
