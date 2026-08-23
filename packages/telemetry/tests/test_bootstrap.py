"""Shared trace and log bootstrap honors standard OTEL behavior safely."""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import pytest
from curie_telemetry import TelemetryRuntime, configure, emit_log_event
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .conftest import OtlpHttpCapture

FAKE_API_KEY = "sk-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"


def _any_value(value: Any) -> object:
    selected = value.WhichOneof("value")
    if selected == "string_value":
        return value.string_value
    if selected == "bool_value":
        return value.bool_value
    if selected == "int_value":
        return value.int_value
    if selected == "double_value":
        return value.double_value
    if selected == "array_value":
        return tuple(_any_value(item) for item in value.array_value.values)
    return None


def _attributes(items: object) -> dict[str, object]:
    return {item.key: _any_value(item.value) for item in items}  # type: ignore[attr-defined]


def _exported_spans(capture: OtlpHttpCapture) -> list[tuple[object, object]]:
    out: list[tuple[object, object]] = []
    for request in capture.traces:
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                out.extend((resource_spans.resource, span) for span in scope_spans.spans)
    return out


def _exported_logs(capture: OtlpHttpCapture) -> list[tuple[object, object]]:
    out: list[tuple[object, object]] = []
    for request in capture.logs:
        for resource_logs in request.resource_logs:
            for scope_logs in resource_logs.scope_logs:
                out.extend((resource_logs.resource, record) for record in scope_logs.log_records)
    return out


def _span_batch(runtime: TelemetryRuntime) -> BatchSpanProcessor:
    provider = runtime.tracer._tracer_provider  # type: ignore[attr-defined]
    processors = provider._active_span_processor._span_processors  # noqa: SLF001
    return next(item for item in processors if isinstance(item, BatchSpanProcessor))


def _log_batch(runtime: TelemetryRuntime) -> BatchLogRecordProcessor:
    assert runtime.log_handler is not None
    provider = runtime.log_handler._logger_provider  # noqa: SLF001
    processors = provider._multi_log_record_processor._log_record_processors  # noqa: SLF001
    return next(item for item in processors if isinstance(item, BatchLogRecordProcessor))


def _batch_settings(processor: object) -> dict[str, object]:
    batch = processor._batch_processor  # type: ignore[attr-defined]  # noqa: SLF001
    return {
        "queue": batch._max_queue_size,  # noqa: SLF001
        "delay": batch._schedule_delay_millis,  # noqa: SLF001
        "batch": batch._max_export_batch_size,  # noqa: SLF001
        "timeout": batch._export_timeout_millis,  # noqa: SLF001
        "exporter_module": type(batch._exporter).__module__,  # noqa: SLF001
    }


def test_no_endpoint_is_a_true_noop_and_never_mutates_root_logging() -> None:
    root = logging.getLogger()
    before = tuple(root.handlers)
    runtime = configure(service_name="curie-api", service_version="1.2.3")

    with runtime.tracer.start_as_current_span("not-recorded") as span:
        assert not span.is_recording()
    assert runtime.log_handler is None
    assert tuple(root.handlers) == before
    assert runtime.force_flush(timeout_millis=10) is True
    assert runtime.shutdown(timeout_millis=10) is True
    assert runtime.shutdown(timeout_millis=10) is True


def test_otel_sdk_disabled_wins_even_when_an_endpoint_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example.test:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    runtime = configure(service_name="curie-api", service_version="1.2.3")
    with runtime.tracer.start_as_current_span("disabled") as span:
        assert not span.is_recording()
    assert runtime.log_handler is None
    runtime.shutdown(timeout_millis=10)


def test_signal_specific_endpoint_gates_are_independent(
    monkeypatch: pytest.MonkeyPatch, otlp_http_capture: OtlpHttpCapture
) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"{otlp_http_capture.endpoint}/v1/traces"
    )
    traces_only = configure(service_name="curie-api", service_version="1.2.3")
    assert traces_only.log_handler is None
    with traces_only.tracer.start_as_current_span("trace-only") as span:
        assert span.is_recording()
    traces_only.shutdown(timeout_millis=1000)

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", f"{otlp_http_capture.endpoint}/v1/logs"
    )
    logs_only = configure(service_name="curie-api", service_version="1.2.3")
    assert logs_only.log_handler is not None
    with logs_only.tracer.start_as_current_span("not-recorded") as span:
        assert not span.is_recording()
    logs_only.shutdown(timeout_millis=1000)


def test_configured_runtime_preserves_console_logs_and_exports_only_curated_records(
    monkeypatch: pytest.MonkeyPatch, otlp_http_capture: OtlpHttpCapture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_http_capture.endpoint)
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment.name=local,curie.session_id=must-not-be-a-resource,"
        "host.name=private-host.example.test",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "must-not-override-caller")

    runtime = configure(service_name="curie-api", service_version="1.2.3")
    root_before = tuple(logging.getLogger().handlers)
    stderr = io.StringIO()
    console = logging.StreamHandler(stderr)
    logger = logging.getLogger("curie_api.telemetry_contract")
    prior_handlers = list(logger.handlers)
    prior_level = logger.level
    prior_propagate = logger.propagate
    logger.handlers = [console]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        runtime.attach_logging(logger)
        runtime.attach_logging(logger)
        assert logger.handlers.count(runtime.log_handler) == 1
        assert tuple(logging.getLogger().handlers) == root_before

        with runtime.tracer.start_as_current_span("GET /health") as span:
            span.set_attribute("http.request.method", "GET")
            span.set_attribute("http.request.header.authorization", FAKE_API_KEY)
            span.set_attribute("error.type", FAKE_API_KEY)
            span_context = span.get_span_context()
            logger.info("request completed credential=%s", FAKE_API_KEY)
            emit_log_event(logger, "http.server.completed")

        assert runtime.force_flush(timeout_millis=1000) is True
    finally:
        runtime.shutdown(timeout_millis=1000)
        logger.handlers = prior_handlers
        logger.setLevel(prior_level)
        logger.propagate = prior_propagate

    console_output = stderr.getvalue()
    assert FAKE_API_KEY not in console_output
    assert "[REDACTED:" in console_output

    spans = _exported_spans(otlp_http_capture)
    logs = _exported_logs(otlp_http_capture)
    assert len(spans) == 1
    assert len(logs) == 1
    span_resource, exported_span = spans[0]
    log_resource, exported_log = logs[0]

    for resource in (span_resource, log_resource):
        resource_attrs = _attributes(resource.attributes)
        assert resource_attrs["service.namespace"] == "curie"
        assert resource_attrs["service.name"] == "curie-api"
        assert resource_attrs["service.version"] == "1.2.3"
        assert resource_attrs["deployment.environment.name"] == "local"
        assert isinstance(resource_attrs["service.instance.id"], str)
        assert resource_attrs["service.instance.id"]
        assert "curie.session_id" not in resource_attrs
        assert "curie.sandbox_id" not in resource_attrs
        assert "host.name" not in resource_attrs

    assert _attributes(span_resource.attributes)["service.instance.id"] == _attributes(
        log_resource.attributes
    )["service.instance.id"]
    span_attrs = _attributes(exported_span.attributes)
    assert span_attrs["http.request.method"] == "GET"
    assert "http.request.header.authorization" not in span_attrs
    assert FAKE_API_KEY not in repr(span_attrs)

    assert exported_log.trace_id == span_context.trace_id.to_bytes(16, "big")
    assert exported_log.span_id == span_context.span_id.to_bytes(8, "big")
    assert FAKE_API_KEY not in repr(exported_log)
    assert _any_value(exported_log.body) == "http.server.completed"


def test_unmarked_arbitrary_diagnostics_never_reach_otel_but_remain_on_stderr(
    monkeypatch: pytest.MonkeyPatch, otlp_http_capture: OtlpHttpCapture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_http_capture.endpoint)
    runtime = configure(service_name="curie-api", service_version="1.2.3")
    logger = logging.getLogger("curie_api.arbitrary_diagnostics")
    stderr = io.StringIO()
    console = logging.StreamHandler(stderr)
    prior_handlers = list(logger.handlers)
    prior_level = logger.level
    prior_propagate = logger.propagate
    logger.handlers = [console]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        runtime.attach_logging(logger)
        with runtime.tracer.start_as_current_span("GET /health"):
            arbitrary_prompt = "prompt fragment: compare the quarterly strategy"
            logger.info(arbitrary_prompt)
            logger.warning(
                "model output: the forecast changed",
                extra={"curie.session_id": "arbitrary-tool-session"},
            )
            try:
                raise RuntimeError("tool exception: calendar lookup failed")
            except RuntimeError:
                logger.exception("tool execution failed")
            with pytest.raises(ValueError, match="unsupported telemetry log event"):
                emit_log_event(logger, arbitrary_prompt)
            emit_log_event(logger, "http.server.completed")
        assert runtime.force_flush(timeout_millis=1000)
    finally:
        runtime.shutdown(timeout_millis=1000)
        logger.handlers = prior_handlers
        logger.setLevel(prior_level)
        logger.propagate = prior_propagate

    diagnostics = stderr.getvalue()
    assert "prompt fragment: compare the quarterly strategy" in diagnostics
    assert "model output: the forecast changed" in diagnostics
    assert "tool exception: calendar lookup failed" in diagnostics
    assert [_any_value(record.body) for _, record in _exported_logs(otlp_http_capture)] == [
        "http.server.completed"
    ]


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
def test_protocol_selection_uses_the_declared_real_exporter_dependencies(
    protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    runtime = configure(service_name="curie-api", service_version="1.2.3")
    span_module = str(_batch_settings(_span_batch(runtime))["exporter_module"])
    log_module = str(_batch_settings(_log_batch(runtime))["exporter_module"])
    expected = ".grpc." if protocol == "grpc" else ".http."
    assert expected in span_module
    assert expected in log_module
    runtime.shutdown(timeout_millis=100)


def test_unset_protocol_defaults_to_http_and_exporters_parse_standard_environment(
    monkeypatch: pytest.MonkeyPatch, otlp_http_capture: OtlpHttpCapture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_http_capture.endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-test-header=safe")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "0.75")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip")
    runtime = configure(service_name="curie-api", service_version="1.2.3")

    span_exporter = _span_batch(runtime)._batch_processor._exporter  # noqa: SLF001
    log_exporter = _log_batch(runtime)._batch_processor._exporter  # noqa: SLF001
    assert ".http." in type(span_exporter).__module__
    assert ".http." in type(log_exporter).__module__
    assert span_exporter._endpoint == f"{otlp_http_capture.endpoint}/v1/traces"  # noqa: SLF001
    assert log_exporter._endpoint == f"{otlp_http_capture.endpoint}/v1/logs"  # noqa: SLF001
    assert span_exporter._headers.get("x-test-header") == "safe"  # noqa: SLF001
    assert log_exporter._headers.get("x-test-header") == "safe"  # noqa: SLF001
    assert span_exporter._timeout == 0.75  # noqa: SLF001
    assert log_exporter._timeout == 0.75  # noqa: SLF001
    runtime.shutdown(timeout_millis=1000)


def test_both_batch_processors_are_bounded_and_subsecond(
    monkeypatch: pytest.MonkeyPatch, otlp_http_capture: OtlpHttpCapture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_http_capture.endpoint)
    runtime = configure(service_name="curie-api", service_version="1.2.3")

    for processor in (_span_batch(runtime), _log_batch(runtime)):
        settings = _batch_settings(processor)
        assert 0 < settings["queue"] <= 2048
        assert 0 < settings["batch"] <= settings["queue"]
        assert 0 < settings["delay"] < 1000
        assert 0 < settings["timeout"] <= 1000

    runtime.shutdown(timeout_millis=1000)


def test_flush_and_shutdown_are_idempotent_and_honor_the_callers_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "30")
    runtime = configure(service_name="curie-api", service_version="1.2.3")
    with runtime.tracer.start_as_current_span("queued-before-shutdown"):
        pass

    started = time.monotonic()
    runtime.shutdown(timeout_millis=200)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8

    repeated = time.monotonic()
    assert runtime.shutdown(timeout_millis=200) is True
    assert time.monotonic() - repeated < 0.1
