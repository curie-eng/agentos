"""Shared OpenTelemetry bootstrap, privacy policy, and propagation for Curie."""

from .attributes import (
    SCHEMA_VERSION,
    attribute_types_for,
    event_names_for,
    log_event_names_for,
    sanitize_attributes,
    set_turn_identity,
)
from .bootstrap import TelemetryRuntime, configure
from .carrier import (
    TRACE_CONTEXT_FIELD,
    extract_http_trace_context,
    extract_trace_context,
    inject_trace_context,
    inject_trace_headers,
)
from .logs import emit_log_event
from .redact import (
    REDACTION_BOUNDARIES,
    REDACTION_RULES,
    RedactingLogFilter,
    RedactionRule,
    install_logging_redaction,
    redact_span_attribute,
    redact_text,
    redact_value,
)

__all__ = [
    "REDACTION_BOUNDARIES",
    "REDACTION_RULES",
    "SCHEMA_VERSION",
    "TRACE_CONTEXT_FIELD",
    "RedactingLogFilter",
    "RedactionRule",
    "TelemetryRuntime",
    "attribute_types_for",
    "configure",
    "event_names_for",
    "emit_log_event",
    "extract_http_trace_context",
    "extract_trace_context",
    "inject_trace_context",
    "inject_trace_headers",
    "install_logging_redaction",
    "log_event_names_for",
    "redact_span_attribute",
    "redact_text",
    "redact_value",
    "sanitize_attributes",
    "set_turn_identity",
]
