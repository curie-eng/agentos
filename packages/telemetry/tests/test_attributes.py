"""The one committed schema remains closed independently for each service."""

from __future__ import annotations

import json
from pathlib import Path

from curie_telemetry.attributes import (
    SchemaValidatingSpanProcessor,
    attribute_types_for,
    event_names_for,
    log_event_names_for,
    sanitize_attributes,
)
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "runner/schema/otel-attributes.schema.json"
SERVICES = ("curie-api", "curie-dispatcher", "curie-worker", "curie-runner")
FAKE_API_KEY = "sk-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
WORKER_EVENTS = frozenset(
    {
        "trace_context.invalid",
        "messaging.ack",
        "messaging.pending",
        "messaging.dead_letter",
        "worker.dedupe.checked",
        "worker.dedupe.skip",
        "worker.lock.wait",
        "worker.lock.acquired",
        "worker.queue.wait",
        "worker.retry.scheduled",
        "worker.retry.stopped",
        "worker.route.start",
        "worker.route.steer",
        "worker.route.finish_race",
        "worker.reply.final",
        "worker.completion.settled",
        "worker.terminal",
    }
)


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text())


def test_committed_artifact_has_shared_and_per_service_partitions() -> None:
    schema = _schema()
    assert schema["schema_version"] == "v1"
    assert isinstance(schema["shared"], dict)
    services = schema["services"]
    assert isinstance(services, dict)
    assert set(services) == set(SERVICES)
    events = schema["events"]
    assert isinstance(events, dict)
    assert set(events) == set(SERVICES)
    log_events = schema["log_events"]
    assert isinstance(log_events, dict)
    assert set(log_events) == set(SERVICES)
    assert {
        service: sorted(event_names_for(service)) for service in SERVICES
    } == events

    runner = {**schema["shared"], **services["curie-runner"]}
    assert {
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "langfuse.trace.name",
        "langfuse.session.id",
        "langfuse.user.id",
        "curie.session_id",
        "curie.sandbox_id",
    } <= set(runner)
    assert "http.request.method" in attribute_types_for("curie-api")
    assert "messaging.system" in attribute_types_for("curie-dispatcher")
    assert attribute_types_for("curie-worker")["curie.turn.outcome"] == "str"
    assert attribute_types_for("curie-worker")["curie.sandbox.outcome"] == "str"
    assert set(runner.values()) <= {"str", "int", "bool"}


def test_worker_event_vocabulary_is_closed_in_the_same_service_artifact() -> None:
    assert WORKER_EVENTS <= event_names_for("curie-worker")
    assert "worker.terminal" not in event_names_for("curie-api")


def test_application_log_vocabulary_and_worker_queue_wait_type_are_closed() -> None:
    assert log_event_names_for("curie-api") == frozenset(
        {"http.server.completed", "http.server.failed"}
    )
    assert log_event_names_for("curie-runner") == frozenset(
        {"agent.run.completed", "agent.run.failed"}
    )
    assert log_event_names_for("curie-dispatcher") == frozenset(
        {"dispatcher.turn.enqueued"}
    )
    assert {
        "worker.trace_context.invalid",
        "worker.turn.completed",
        "worker.turn.failed",
    } <= log_event_names_for("curie-worker")
    assert "worker.turn.completed" not in log_event_names_for("curie-api")
    assert attribute_types_for("curie-worker")["curie.queue.wait_ms"] == "int"
    assert sanitize_attributes("curie-worker", {"curie.queue.wait_ms": 17}) == {
        "curie.queue.wait_ms": 17
    }
    assert sanitize_attributes("curie-worker", {"curie.queue.wait_ms": True}) == {}
    schema = _schema()
    assert {
        service: sorted(log_event_names_for(service)) for service in SERVICES
    } == schema["log_events"]


def test_service_allowlist_is_shared_plus_its_partition_only() -> None:
    schema = _schema()
    services = schema["services"]
    assert isinstance(services, dict)
    shared = schema["shared"]
    assert isinstance(shared, dict)

    for service_name in SERVICES:
        expected = {**shared, **services[service_name]}
        assert attribute_types_for(service_name) == expected


def test_sanitizer_rejects_another_services_valid_key_and_unknown_keys() -> None:
    api_types = attribute_types_for("curie-api")
    runner_types = attribute_types_for("curie-runner")
    runner_only = next(key for key in runner_types if key not in api_types)
    safe_api_key = next(key for key, value_type in api_types.items() if value_type == "str")

    sanitized = sanitize_attributes(
        "curie-api",
        {
            safe_api_key: "safe",
            runner_only: "otherwise valid",
            "unknown.attribute": "must not export",
        },
    )

    assert sanitized[safe_api_key] == "safe"
    assert runner_only not in sanitized
    assert "unknown.attribute" not in sanitized


def test_declared_types_are_exact_so_bool_never_passes_as_int() -> None:
    schema = _schema()
    services = schema["services"]
    assert isinstance(services, dict)
    bool_case = next(
        (service, key)
        for service, keys in services.items()
        for key, value_type in keys.items()
        if value_type == "bool"
    )
    service, key = bool_case

    assert sanitize_attributes(service, {key: True}) == {key: True}
    assert sanitize_attributes(service, {key: 1}) == {}

    int_case = next(
        (candidate, attr)
        for candidate in SERVICES
        for attr, value_type in attribute_types_for(candidate).items()
        if value_type == "int"
    )
    int_service, int_key = int_case
    assert sanitize_attributes(int_service, {int_key: 7}) == {int_key: 7}
    assert sanitize_attributes(int_service, {int_key: True}) == {}


def test_sanitizer_drops_recursively_leaking_values_instead_of_exporting_them() -> None:
    key = next(
        key
        for key, value_type in attribute_types_for("curie-runner").items()
        if value_type == "str"
    )
    assert sanitize_attributes("curie-runner", {key: FAKE_API_KEY}) == {}
    assert sanitize_attributes("curie-runner", {key: ["safe", FAKE_API_KEY]}) == {}


def test_export_processor_strips_bypasses_before_the_exporter_sees_them() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SchemaValidatingSpanProcessor("curie-api"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("schema-backstop")

    with tracer.start_as_current_span("GET /health") as span:
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("gen_ai.request.model", "runner-only")
        span.set_attribute("unknown.attribute", "not closed")
        span.set_attribute("error.type", FAKE_API_KEY)

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes["http.request.method"] == "GET"
    assert "gen_ai.request.model" not in finished.attributes
    assert "unknown.attribute" not in finished.attributes
    assert FAKE_API_KEY not in repr(dict(finished.attributes or {}))
    provider.shutdown()


def test_sdk_private_attribute_surface_is_compatible_with_export_mutation() -> None:
    attributes = BoundedAttributes(attributes={"http.request.method": "GET"})
    assert hasattr(attributes, "_immutable"), (
        "OpenTelemetry BoundedAttributes no longer exposes _immutable; the "
        "export backstop must be adapted before the SDK bound changes"
    )
    assert hasattr(attributes, "__delitem__")
