"""Dispatcher producer tracing across the real Valkey boundary."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

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
    assert "langfuse.session.id" not in producer.attributes
    assert "langfuse.trace.name" not in producer.attributes
    assert "langfuse.user.id" not in producer.attributes

    _, fields = redis_client.xrange(config.stream)[0]
    carrier = json.loads(fields[TRACE_CONTEXT_FIELD])
    assert carrier["traceparent"].startswith(
        f"00-{producer.context.trace_id:032x}-{producer.context.span_id:016x}-"
    )
    exported = repr(dict(producer.attributes or {}))
    assert "Ev-otel" not in exported
    assert CHANNEL not in exported
    assert "safe placeholder request" not in exported


def test_message_claim_placeholder_xadd_order_producer_parentage_and_curated_log(
    redis_client: redis.Redis,
    config: DispatcherConfig,
) -> None:
    order: list[str] = []
    provider, exporter = _provider()
    tracer = provider.get_tracer("dispatcher-handler")
    publisher = _OrderedPublisher(redis_client, order)
    logger = logging.getLogger("curie_dispatcher.telemetry-test")

    with patch("curie_dispatcher.handlers.emit_log_event") as emit:
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
            config=config,
            clock=lambda: "2026-08-23T00:00:00+00:00",
            logger=logger,
            tracer=tracer,
        )

    assert stream_id
    assert order == ["claim", "placeholder", "xadd"]
    emit.assert_called_once_with(
        logging.getLogger("curie_dispatcher.handlers"),
        "dispatcher.turn.enqueued",
    )
    _assert_producer(exporter, redis_client, config)
    provider.shutdown()


def test_block_action_uses_the_same_producer_and_transport_metadata_path(
    redis_client: redis.Redis,
    config: DispatcherConfig,
) -> None:
    order: list[str] = []
    provider, exporter = _provider()
    tracer = provider.get_tracer("dispatcher-action")
    publisher = _OrderedPublisher(redis_client, order)
    logger = logging.getLogger("curie_dispatcher.telemetry-action-test")

    with patch("curie_dispatcher.handlers.emit_log_event") as emit:
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
            config=config,
            clock=lambda: "2026-08-23T00:00:00+00:00",
            logger=logger,
            tracer=tracer,
        )

    assert stream_id
    assert order == ["claim", "placeholder", "xadd"]
    emit.assert_called_once_with(
        logging.getLogger("curie_dispatcher.handlers"),
        "dispatcher.turn.enqueued",
    )
    _assert_producer(exporter, redis_client, config)
    provider.shutdown()


def test_dispatch_never_performs_a_post_enqueue_agent_lookup_or_rejects_custom_logger(
    redis_client: redis.Redis,
    config: DispatcherConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _provider()
    tracer = provider.get_tracer("dispatcher-lookup-failure")
    lookup = MagicMock(side_effect=AssertionError("dispatcher performed HTTP lookup"))
    monkeypatch.setattr("httpx.get", lookup)

    stream_id = process_event(
        body={"event_id": "Ev-otel-no-lookup"},
        event={
            "type": "app_mention",
            "channel": CHANNEL,
            "user": "U0EXAMPLE1",
            "text": "safe placeholder request",
            "ts": THREAD,
        },
        web_client=_web_client([]),
        redis_client=redis_client,
        config=config,
        clock=lambda: "2026-08-23T00:00:00+00:00",
        logger=logging.getLogger("external.application.diagnostics"),
        tracer=tracer,
    )

    assert stream_id
    assert redis_client.xlen(config.stream) == 1
    lookup.assert_not_called()
    provider.shutdown()
