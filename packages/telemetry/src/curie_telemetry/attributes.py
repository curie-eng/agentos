"""Closed per-service OTEL attribute and event vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status
from opentelemetry.util.types import AttributeValue

from .redact import redact_value, still_leaks

SCHEMA_VERSION = "v1"

_SHARED: dict[str, str] = {
    "curie.sandbox_id": "str",
    "curie.session_id": "str",
    "deployment.environment.name": "str",
    "langfuse.session.id": "str",
    "langfuse.trace.name": "str",
    "langfuse.user.id": "str",
    "schema.version": "str",
    "service.instance.id": "str",
    "service.name": "str",
    "service.namespace": "str",
    "service.version": "str",
}

_MESSAGING: dict[str, str] = {
    "messaging.destination.name": "str",
    "messaging.operation.type": "str",
    "messaging.system": "str",
}

_HTTP: dict[str, str] = {
    "http.request.method": "str",
    "http.response.status_code": "int",
    "server.address": "str",
    "server.port": "int",
}

_ERROR: dict[str, str] = {
    "error.type": "str",
    "exception.escaped": "bool",
    "exception.type": "str",
}

_SERVICES: dict[str, dict[str, str]] = {
    "curie-api": {
        **_HTTP,
        **_MESSAGING,
        **_ERROR,
    },
    "curie-dispatcher": {
        **_MESSAGING,
        **_ERROR,
    },
    "curie-worker": {
        **_HTTP,
        **_MESSAGING,
        **_ERROR,
        "curie.sandbox.outcome": "str",
        "curie.turn.outcome": "str",
    },
    "curie-runner": {
        **_ERROR,
        "gen_ai.approval.decision": "str",
        "gen_ai.operation.name": "str",
        "gen_ai.request.model": "str",
        "gen_ai.tool.name": "str",
        "gen_ai.usage.cache_creation_input_tokens": "int",
        "gen_ai.usage.cache_read_input_tokens": "int",
        "gen_ai.usage.input_tokens": "int",
        "gen_ai.usage.output_tokens": "int",
        "model": "str",
    },
}

_EVENTS: dict[str, frozenset[str]] = {
    "curie-api": frozenset(
        {
            "messaging.enqueued",
            "trace_context.invalid",
        }
    ),
    "curie-dispatcher": frozenset(
        {
            "dispatcher.agent_lookup.resolved",
            "dispatcher.agent_lookup.unavailable",
            "dispatcher.dedupe.checked",
            "dispatcher.dedupe.skip",
            "dispatcher.placeholder.posted",
            "messaging.enqueued",
        }
    ),
    "curie-worker": frozenset(
        {
            "messaging.ack",
            "messaging.dead_letter",
            "messaging.pending",
            "trace_context.invalid",
            "worker.completion.settled",
            "worker.dedupe.checked",
            "worker.dedupe.skip",
            "worker.lock.acquired",
            "worker.lock.wait",
            "worker.reply.final",
            "worker.route.finish_race",
            "worker.route.start",
            "worker.route.steer",
            "worker.terminal",
        }
    ),
    "curie-runner": frozenset(),
}

_EXACT_TYPES: dict[str, type[object]] = {
    "str": str,
    "int": int,
    "bool": bool,
}


def _require_service(service_name: str) -> None:
    if service_name not in _SERVICES:
        expected = ", ".join(sorted(_SERVICES))
        raise ValueError(
            f"unsupported telemetry service {service_name!r}; expected one of {expected}"
        )


def shared_attribute_types() -> dict[str, str]:
    """Return a copy of the platform-wide safe attribute partition."""

    return dict(_SHARED)


def service_attribute_types() -> dict[str, dict[str, str]]:
    """Return copies of every service-owned attribute partition."""

    return {service: dict(values) for service, values in _SERVICES.items()}


def service_event_names() -> dict[str, frozenset[str]]:
    """Return copies of the service-owned event vocabularies."""

    return {service: frozenset(values) for service, values in _EVENTS.items()}


def attribute_types_for(service_name: str) -> dict[str, str]:
    """Return only ``shared`` plus the named service's closed partition."""

    _require_service(service_name)
    return {**_SHARED, **_SERVICES[service_name]}


def event_names_for(service_name: str) -> frozenset[str]:
    """Return the exact event vocabulary exported by one service."""

    _require_service(service_name)
    return _EVENTS[service_name]


def sanitize_attributes(
    service_name: str, attributes: Mapping[str, object] | None
) -> dict[str, AttributeValue]:
    """Drop unknown, wrong-type, or recursively leaking attributes."""

    if not attributes:
        return {}
    declared = attribute_types_for(service_name)
    sanitized: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        expected_name = declared.get(key)
        if expected_name is None or still_leaks(value):
            continue
        expected_type = _EXACT_TYPES[expected_name]
        if type(value) is not expected_type:
            continue
        sanitized[key] = cast(AttributeValue, redact_value(value))
    return sanitized


def set_turn_identity(
    span: Any,
    *,
    agent_id: object,
    conversation_id: str,
    user_id: str | None = None,
) -> str:
    """Stamp the exact incumbent Langfuse name/session/user identity."""

    session_id = f"agent-{agent_id}-thread-{conversation_id}"
    span.set_attribute("langfuse.trace.name", f"curie-run:{session_id}")
    span.set_attribute("langfuse.session.id", session_id)
    if user_id:
        span.set_attribute("langfuse.user.id", user_id)
    return session_id


def _sanitize_bounded_attributes(
    service_name: str, attributes: BoundedAttributes
) -> None:
    clean = sanitize_attributes(service_name, dict(attributes))
    was_immutable = attributes._immutable  # noqa: SLF001
    attributes._immutable = False  # noqa: SLF001
    try:
        for key in list(attributes.keys()):
            del attributes[key]
        for key, value in clean.items():
            attributes[key] = value
    finally:
        attributes._immutable = was_immutable  # noqa: SLF001


class SchemaValidatingSpanProcessor(SpanProcessor):
    """Export-time privacy backstop for spans and their event vocabulary."""

    def __init__(self, service_name: str) -> None:
        _require_service(service_name)
        self._service_name = service_name
        self._events = event_names_for(service_name)

    def on_start(
        self, span: Span, parent_context: Context | None = None
    ) -> None:
        del span, parent_context

    def on_end(self, span: ReadableSpan) -> None:
        raw = span._attributes  # noqa: SLF001
        if isinstance(raw, BoundedAttributes):
            _sanitize_bounded_attributes(self._service_name, raw)

        kept: list[Event] = []
        for event in span.events:
            if event.name not in self._events:
                continue
            attributes = sanitize_attributes(self._service_name, event.attributes)
            kept.append(Event(event.name, attributes=attributes, timestamp=event.timestamp))
        span._events = tuple(kept)  # noqa: SLF001

        # SDK-generated status descriptions include exception messages. The
        # status code and closed ``error.type`` retain the useful classification.
        if span.status.description is not None:
            span._status = Status(span.status.status_code)  # noqa: SLF001

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


class SchemaValidatingLogRecordProcessor(LogRecordProcessor):
    """Redact bodies and close log attributes before the batch exporter."""

    def __init__(self, service_name: str) -> None:
        _require_service(service_name)
        self._service_name = service_name

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        record.body = cast(Any, redact_value(record.body))
        raw = record.attributes
        if isinstance(raw, BoundedAttributes):
            _sanitize_bounded_attributes(self._service_name, raw)
        else:
            record.attributes = sanitize_attributes(self._service_name, raw)

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True
