"""Shared OpenTelemetry bootstrap, privacy policy, and propagation for Curie."""

from .attributes import (
    SCHEMA_VERSION,
    attribute_types_for,
    event_names_for,
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
    "extract_http_trace_context",
    "extract_trace_context",
    "inject_trace_context",
    "inject_trace_headers",
    "install_logging_redaction",
    "redact_span_attribute",
    "redact_text",
    "redact_value",
    "sanitize_attributes",
    "set_turn_identity",
]
