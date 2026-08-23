"""Dispatcher producer tracing across the real Valkey boundary."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import redis
from aci_protocol import QueuedTurn, ReplyHandle
from curie_dispatcher.config import DispatcherConfig
from curie_dispatcher.handlers import process_action, process_event
from curie_dispatcher.queue import enqueue, from_stream_fields, to_stream_fields
from curie_telemetry.carrier import TRACE_CONTEXT_FIELD
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from slack_sdk.web import WebClient

AGENT_ID = "11111111-1111-1111-1111-111111111111"
CHANNEL = "C0EXAMPLE1"
THREAD = "1700.0001"
BOT_TS = "1700.0002"


def _turn() -> QueuedTurn:
    return QueuedTurn(
        event_id="Ev-otel-queue",
        conversation_id=THREAD,
        author="U0EXAMPLE1",
        text="safe placeholder request",
        reply_handle=ReplyHandle(kind="slack", channel=CHANNEL, placeholder=BOT_TS),
        received_at="2026-08-23T00:00:00+00:00",
    )


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_to_stream_fields_injects_only_optional_transport_context() -> None:
    provider, _ = _provider()
    tracer = provider.get_tracer("dispatcher-queue-test")
    turn = _turn()

    with tracer.start_as_current_span("producer") as span:
        fields = to_stream_fields(turn)
        context = span.get_span_context()

    assert set(fields) == {"payload", TRACE_CONTEXT_FIELD}
    assert fields["payload"] == turn.model_dump_json()
    assert QueuedTurn.model_validate_json(fields["payload"]) == turn
    carrier = json.loads(fields[TRACE_CONTEXT_FIELD])
    assert set(carrier) <= {"traceparent", "tracestate"}
    assert carrier["traceparent"].startswith(
        f"00-{context.trace_id:032x}-{context.span_id:016x}-"
    )
    assert TRACE_CONTEXT_FIELD not in json.loads(fields["payload"])
    provider.shutdown()


def test_payload_only_legacy_entry_remains_valid_and_extra_metadata_is_ignored() -> None:
    turn = _turn()
    assert to_stream_fields(turn) == {"payload": turn.model_dump_json()}
    assert from_stream_fields({"payload": turn.model_dump_json()}) == turn
    assert (
        from_stream_fields(
            {
                "payload": turn.model_dump_json(),
                TRACE_CONTEXT_FIELD: '{"traceparent":"00-invalid"}',
            }
        )
        == turn
    )


def test_enqueue_writes_payload_and_current_carrier_atomically_to_real_valkey(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    provider, _ = _provider()
    tracer = provider.get_tracer("dispatcher-real-valkey")
    turn = _turn()
    with tracer.start_as_current_span("producer"):
        stream_id = enqueue(redis_client, config, turn)

    entries = redis_client.xrange(config.stream)
    assert len(entries) == 1
    entry_id, fields = entries[0]
    assert entry_id == stream_id
    assert set(fields) == {"payload", TRACE_CONTEXT_FIELD}
    assert fields["payload"] == turn.model_dump_json()
    assert from_stream_fields(fields) == turn
    provider.shutdown()


class _OrderedPublisher:
    """Record calls while still delegating every broker operation to Valkey."""

    def __init__(self, client: redis.Redis, order: list[str]) -> None:
        self._client = client
        self._order = order

    def set(self, *args: object, **kwargs: object) -> object:
        self._order.append("claim")
        return self._client.set(*args, **kwargs)

    def xadd(self, *args: object, **kwargs: object) -> object:
        self._order.append("xadd")
        return self._client.xadd(*args, **kwargs)


class _AgentLookupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, redis_client: redis.Redis, stream: str, order: list[str]
    ) -> None:
        self.redis_client = redis_client
        self.stream = stream
        self.order = order
        super().__init__(("127.0.0.1", 0), _agent_handler())


def _agent_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            server = self.server
            assert isinstance(server, _AgentLookupServer)
            assert self.path.startswith("/agents")
            assert self.headers.get("X-API-Key") == "test-api-key"
            assert server.redis_client.xlen(server.stream) == 1
            server.order.append("lookup")
            body = json.dumps(
                [
                    {
                        "id": AGENT_ID,
                        "channel": {"kind": "slack", "address": CHANNEL},
                    }
                ]
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@pytest.fixture
def agent_lookup_server(
    redis_client: redis.Redis, config: DispatcherConfig
) -> Iterator[tuple[_AgentLookupServer, list[str]]]:
    order: list[str] = []
    server = _AgentLookupServer(redis_client, config.stream, order)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, order
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _traced_config(config: DispatcherConfig, server: _AgentLookupServer) -> DispatcherConfig:
    host, port = server.server_address
    return config.model_copy(
        update={
            "api_base_url": f"http://{host}:{port}",
            "api_key": "test-api-key",
        }
    )


def _web_client(order: list[str]) -> WebClient:
    client = WebClient(token="xoxb-test")

    def _placeholder(**_kwargs: object) -> dict[str, str]:
        order.append("placeholder")
        return {"ts": BOT_TS}

    client.chat_postMessage = MagicMock(side_effect=_placeholder)  # type: ignore[method-assign]
    return client


def _assert_producer(
    exporter: InMemorySpanExporter,
    redis_client: redis.Redis,
    config: DispatcherConfig,
) -> None:
    spans = exporter.get_finished_spans()
    producer = next(span for span in spans if span.name == "send curie:runs")
    assert producer.kind is SpanKind.PRODUCER
    assert producer.attributes["messaging.system"] == "valkey"
    assert producer.attributes["messaging.destination.name"] == config.stream
    assert producer.attributes["messaging.operation.type"] == "send"
    session_id = f"agent-{AGENT_ID}-thread-{THREAD}"
    assert producer.attributes["langfuse.session.id"] == session_id
    assert producer.attributes["langfuse.trace.name"] == f"curie-run:{session_id}"
    assert producer.attributes["langfuse.user.id"] == "U0EXAMPLE1"

    _, fields = redis_client.xrange(config.stream)[0]
    carrier = json.loads(fields[TRACE_CONTEXT_FIELD])
    assert carrier["traceparent"].startswith(
        f"00-{producer.context.trace_id:032x}-{producer.context.span_id:016x}-"
    )
    exported = repr(dict(producer.attributes or {}))
    assert "Ev-otel" not in exported
    assert CHANNEL not in exported
    assert "safe placeholder request" not in exported


def test_message_claim_placeholder_xadd_lookup_order_and_producer_parentage(
    redis_client: redis.Redis,
    config: DispatcherConfig,
    agent_lookup_server: tuple[_AgentLookupServer, list[str]],
) -> None:
    server, order = agent_lookup_server
    traced_config = _traced_config(config, server)
    provider, exporter = _provider()
    tracer = provider.get_tracer("dispatcher-handler")
    publisher = _OrderedPublisher(redis_client, order)

    stream_id = process_event(
        body={"event_id": "Ev-otel-message"},
        event={
            "type": "app_mention",
            "channel": CHANNEL,
            "user": "U0EXAMPLE1",
            "text": "safe placeholder request",
            "ts": THREAD,
        },
        web_client=_web_client(order),
        redis_client=publisher,  # type: ignore[arg-type]
        config=traced_config,
        clock=lambda: "2026-08-23T00:00:00+00:00",
        tracer=tracer,
    )

    assert stream_id
    assert order == ["claim", "placeholder", "xadd", "lookup"]
    _assert_producer(exporter, redis_client, traced_config)
    provider.shutdown()


def test_block_action_uses_the_same_producer_and_transport_metadata_path(
    redis_client: redis.Redis,
    config: DispatcherConfig,
    agent_lookup_server: tuple[_AgentLookupServer, list[str]],
) -> None:
    server, order = agent_lookup_server
    traced_config = _traced_config(config, server)
    provider, exporter = _provider()
    tracer = provider.get_tracer("dispatcher-action")
    publisher = _OrderedPublisher(redis_client, order)

    stream_id = process_action(
        body={
            "trigger_id": "trigger-otel",
            "channel": {"id": CHANNEL},
            "user": {"id": "U0EXAMPLE1"},
            "message": {"ts": THREAD, "thread_ts": THREAD},
            "actions": [{"action_id": "reports", "action_ts": "1.5"}],
        },
        web_client=_web_client(order),
        redis_client=publisher,  # type: ignore[arg-type]
        config=traced_config,
        clock=lambda: "2026-08-23T00:00:00+00:00",
        tracer=tracer,
    )

    assert stream_id
    assert order == ["claim", "placeholder", "xadd", "lookup"]
    _assert_producer(exporter, redis_client, traced_config)
    provider.shutdown()


def test_agent_lookup_failure_never_rolls_back_the_durable_enqueue(
    redis_client: redis.Redis,
    config: DispatcherConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _ = _provider()
    tracer = provider.get_tracer("dispatcher-lookup-failure")
    unavailable = config.model_copy(
        update={"api_base_url": "http://127.0.0.1:1", "api_key": "test-api-key"}
    )

    with caplog.at_level("WARNING"):
        stream_id = process_event(
            body={"event_id": "Ev-otel-lookup-failure"},
            event={
                "type": "app_mention",
                "channel": CHANNEL,
                "user": "U0EXAMPLE1",
                "text": "safe placeholder request",
                "ts": THREAD,
            },
            web_client=_web_client([]),
            redis_client=redis_client,
            config=unavailable,
            clock=lambda: "2026-08-23T00:00:00+00:00",
            tracer=tracer,
        )

    assert stream_id
    assert redis_client.xlen(config.stream) == 1
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "agent lookup" in logged.lower()
    assert CHANNEL not in logged
    provider.shutdown()
