"""Bounded, explicit OTEL trace and log bootstrap for one Curie service."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol, cast
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

from .attributes import (
    SCHEMA_VERSION,
    SchemaValidatingLogRecordProcessor,
    SchemaValidatingSpanProcessor,
    attribute_types_for,
)
from .redact import RedactingLogFilter, install_logging_redaction

_MAX_QUEUE_SIZE = 2048
_MAX_EXPORT_BATCH_SIZE = 256
_SCHEDULE_DELAY_MILLIS = 250
_EXPORT_TIMEOUT_MILLIS = 750
_MAX_SHUTDOWN_MILLIS = 2000

_INSTANCE_LOCK = threading.Lock()
_INSTANCE_PID = 0
_INSTANCE_ID = ""


class _ProviderInspectableTracer(Protocol):
    _tracer_provider: TracerProvider


def _service_instance_id() -> str:
    global _INSTANCE_ID, _INSTANCE_PID
    pid = os.getpid()
    with _INSTANCE_LOCK:
        if _INSTANCE_PID != pid or not _INSTANCE_ID:
            _INSTANCE_PID = pid
            _INSTANCE_ID = str(uuid.uuid4())
        return _INSTANCE_ID


def _deployment_environment() -> str | None:
    raw = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
    for item in raw.split(","):
        try:
            key, value = item.split("=", 1)
        except ValueError:
            continue
        if key.strip() == "deployment.environment.name":
            resolved = unquote(value.strip())
            return resolved or None
    return None


def service_resource(service_name: str, service_version: str) -> Resource:
    """Build the closed process resource shared by every Curie emitter."""

    attributes: dict[str, str] = {
        "schema.version": SCHEMA_VERSION,
        "service.instance.id": _service_instance_id(),
        "service.name": service_name,
        "service.namespace": "curie",
        "service.version": service_version,
    }
    environment = _deployment_environment()
    if environment:
        attributes["deployment.environment.name"] = environment
    return Resource(attributes)


def _disabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def _endpoint(signal: str) -> str:
    specific_name = f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT"
    if specific_name in os.environ:
        return os.environ[specific_name].strip()
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()


def _protocol(signal: str) -> str:
    specific = os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL", "").strip()
    generic = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip()
    return specific or generic or "http/protobuf"


def _trace_exporter(protocol: str) -> Any:
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcOTLPSpanExporter,
        )

        return GrpcOTLPSpanExporter()
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpOTLPSpanExporter,
    )

    return HttpOTLPSpanExporter()


def _log_exporter(protocol: str) -> Any:
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GrpcOTLPLogExporter,
        )

        return GrpcOTLPLogExporter()
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as HttpOTLPLogExporter,
    )

    return HttpOTLPLogExporter()


def _tracer_provider(
    service_name: str, resource: Resource
) -> TracerProvider | None:
    if not _endpoint("traces"):
        return None
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_span_processor(SchemaValidatingSpanProcessor(service_name))
    provider.add_span_processor(
        BatchSpanProcessor(
            _trace_exporter(_protocol("traces")),
            max_queue_size=_MAX_QUEUE_SIZE,
            schedule_delay_millis=_SCHEDULE_DELAY_MILLIS,
            max_export_batch_size=_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=_EXPORT_TIMEOUT_MILLIS,
        )
    )
    return provider


def _logger_provider(
    service_name: str, resource: Resource
) -> LoggerProvider | None:
    if not _endpoint("logs"):
        return None
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_log_record_processor(SchemaValidatingLogRecordProcessor(service_name))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            _log_exporter(_protocol("logs")),
            max_queue_size=_MAX_QUEUE_SIZE,
            schedule_delay_millis=_SCHEDULE_DELAY_MILLIS,
            max_export_batch_size=_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=_EXPORT_TIMEOUT_MILLIS,
        )
    )
    return provider


class _ServiceLogFilter(logging.Filter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._prefixes = (
            service_name,
            service_name.replace("-", "_"),
        )

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)


def _bounded_calls(
    calls: tuple[Callable[[], object], ...], timeout_millis: int
) -> bool:
    if not calls:
        return True
    deadline = time.monotonic() + max(timeout_millis, 0) / 1000
    outcomes: list[object | BaseException | None] = [None] * len(calls)

    def invoke(index: int, call: Callable[[], object]) -> None:
        try:
            outcomes[index] = call()
        except BaseException as exc:  # provider teardown must remain fail open
            outcomes[index] = exc

    threads = [
        threading.Thread(target=invoke, args=(index, call), daemon=True)
        for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        return False
    return all(not isinstance(result, BaseException) and result is not False for result in outcomes)


class TelemetryRuntime:
    """One service's explicit tracer/log providers and bounded lifecycle."""

    def __init__(
        self,
        *,
        service_name: str,
        tracer: Tracer,
        tracer_provider: TracerProvider | None,
        logger_provider: LoggerProvider | None,
        log_handler: LoggingHandler | None,
    ) -> None:
        self.service_name = service_name
        self.tracer = tracer
        self.log_handler = log_handler
        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._attached: list[logging.Logger] = []
        self._level_overrides: dict[logging.Logger, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._shutdown_started = False

    def attach_logging(
        self,
        logger: logging.Logger,
        *,
        default_level: int | None = None,
    ) -> None:
        """Keep diagnostics and attach the scoped OTEL handler once.

        ``default_level`` only replaces an inherited, stricter threshold. An
        operator's explicit logger level always wins, and any temporary default
        is restored at shutdown. This lets service lifecycle records remain
        observable in an embedding that never configured root logging without
        weakening an intentional service-specific threshold.
        """

        install_logging_redaction(logger)
        handler = self.log_handler
        if handler is None:
            return
        with self._lock:
            if self._shutdown_started:
                return
            if not any(
                isinstance(item, RedactingLogFilter) for item in handler.filters
            ):
                handler.addFilter(RedactingLogFilter())
            if not any(
                isinstance(item, _ServiceLogFilter) for item in handler.filters
            ):
                handler.addFilter(_ServiceLogFilter(self.service_name))
            if (
                default_level is not None
                and logger.level == logging.NOTSET
                and logger.getEffectiveLevel() > default_level
            ):
                self._level_overrides[logger] = (logger.level, default_level)
                logger.setLevel(default_level)
            if handler not in logger.handlers:
                logger.addHandler(handler)
            if logger not in self._attached:
                self._attached.append(logger)

    def force_flush(self, *, timeout_millis: int = _MAX_SHUTDOWN_MILLIS) -> bool:
        """Flush both signal queues within one aggregate hard deadline."""

        with self._lock:
            if self._shutdown_started:
                return True
        bounded = min(max(timeout_millis, 0), _MAX_SHUTDOWN_MILLIS)
        calls: list[Callable[[], object]] = []
        tracer_provider = self._tracer_provider
        if tracer_provider is not None:
            calls.append(lambda: tracer_provider.force_flush(bounded))
        logger_provider = self._logger_provider
        if logger_provider is not None:
            calls.append(lambda: logger_provider.force_flush(bounded))
        return _bounded_calls(tuple(calls), bounded)

    def shutdown(self, *, timeout_millis: int = _MAX_SHUTDOWN_MILLIS) -> bool:
        """Flush and close both providers once, never past two seconds total."""

        with self._lock:
            if self._shutdown_started:
                return True
            self._shutdown_started = True
            attached = tuple(self._attached)
            self._attached.clear()
            level_overrides = dict(self._level_overrides)
            self._level_overrides.clear()
        if self.log_handler is not None:
            for logger in attached:
                if self.log_handler in logger.handlers:
                    logger.removeHandler(self.log_handler)
        for logger, (original, applied) in level_overrides.items():
            # Do not overwrite a level an embedding deliberately changed after
            # attachment; only undo the exact temporary default we installed.
            if logger.level == applied:
                logger.setLevel(original)

        bounded = min(max(timeout_millis, 0), _MAX_SHUTDOWN_MILLIS)
        calls: list[Callable[[], object]] = []
        if self._tracer_provider is not None:
            calls.append(self._tracer_provider.shutdown)
        if self._logger_provider is not None:
            calls.append(self._logger_provider.shutdown)
        return _bounded_calls(tuple(calls), bounded)


def configure(*, service_name: str, service_version: str) -> TelemetryRuntime:
    """Build explicit trace/log providers, or a true no-endpoint no-op."""

    # Validate the caller even in no-op mode. A typo must not silently switch a
    # service onto another partition when an endpoint is later configured.
    attribute_types_for(service_name)
    disabled = _disabled()
    traces_enabled = bool(_endpoint("traces")) and not disabled
    logs_enabled = bool(_endpoint("logs")) and not disabled
    if not traces_enabled and not logs_enabled:
        tracer = trace.NoOpTracerProvider().get_tracer(service_name, service_version)
        return TelemetryRuntime(
            service_name=service_name,
            tracer=tracer,
            tracer_provider=None,
            logger_provider=None,
            log_handler=None,
        )

    resource = service_resource(service_name, service_version)
    tracer_provider = _tracer_provider(service_name, resource) if traces_enabled else None
    logger_provider = _logger_provider(service_name, resource) if logs_enabled else None
    tracer = (
        tracer_provider.get_tracer(service_name, service_version)
        if tracer_provider is not None
        else trace.NoOpTracerProvider().get_tracer(service_name, service_version)
    )
    if tracer_provider is not None:
        # SDK 1.44 no longer retains the owning provider on the returned tracer.
        # Keep it reachable for bounded-runtime inspection without installing a
        # process-global provider; TelemetryRuntime still owns its lifecycle.
        cast(_ProviderInspectableTracer, tracer)._tracer_provider = tracer_provider
    handler = (
        LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        if logger_provider is not None
        else None
    )
    return TelemetryRuntime(
        service_name=service_name,
        tracer=tracer,
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
        log_handler=handler,
    )
