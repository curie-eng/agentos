"""Bounded W3C trace-context propagation for Stream metadata and HTTP."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, MutableMapping

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import CarrierT
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACE_CONTEXT_FIELD = "trace_context"

_ALLOWED_KEYS = frozenset({"traceparent", "tracestate"})
_MAX_CARRIER_BYTES = 4096
_MAX_INJECTED_CARRIER_BYTES = 512
_PROPAGATOR = TraceContextTextMapPropagator()
_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_TRACESTATE_SIMPLE_KEY_RE = re.compile(r"^[a-z0-9][_0-9a-z\-*/]{0,255}$")
_TRACESTATE_MULTI_KEY_RE = re.compile(
    r"^[a-z0-9][_0-9a-z\-*/]{0,240}@[a-z][_0-9a-z\-*/]{0,13}$"
)


def _warn(logger: logging.Logger | None) -> None:
    (logger or logging.getLogger(__name__)).warning("ignored malformed trace context")


def _valid_traceparent(value: str) -> bool:
    match = _TRACEPARENT_RE.fullmatch(value)
    return bool(
        match
        and match.group(1) != "0" * 32
        and match.group(2) != "0" * 16
    )


def _valid_tracestate(value: str) -> bool:
    """Validate W3C tracestate before the SDK can diagnose its raw value."""

    if not value or len(value) > 512:
        return False
    members = [member.strip(" \t") for member in value.split(",")]
    if len(members) > 32 or any(not member for member in members):
        return False
    seen: set[str] = set()
    for member in members:
        if "=" not in member:
            return False
        key, member_value = member.split("=", 1)
        if not (
            _TRACESTATE_SIMPLE_KEY_RE.fullmatch(key)
            or _TRACESTATE_MULTI_KEY_RE.fullmatch(key)
        ):
            return False
        if key in seen:
            return False
        seen.add(key)
        if (
            not member_value
            or len(member_value) > 256
            or member_value.endswith(" ")
            or any(
                char in ",=" or ord(char) < 0x20 or ord(char) > 0x7E
                for char in member_value
            )
        ):
            return False
    return True


def _valid_carrier(carrier: Mapping[str, str]) -> bool:
    traceparent = carrier.get("traceparent")
    tracestate = carrier.get("tracestate")
    return bool(
        traceparent is not None
        and _valid_traceparent(traceparent)
        and (tracestate is None or _valid_tracestate(tracestate))
    )


def inject_trace_headers(
    headers: MutableMapping[str, str] | None = None,
    *,
    context: Context | None = None,
) -> dict[str, str]:
    """Inject only W3C trace headers, returning a plain string dictionary."""

    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier, context=context)
    bounded = {
        key: value
        for key, value in carrier.items()
        if key in _ALLOWED_KEYS and isinstance(value, str)
    }
    if headers is not None:
        headers.update(bounded)
        return dict(headers)
    return bounded


def inject_trace_context(context: Context | None = None) -> str | None:
    """Serialize the valid current W3C carrier for a Stream field, if any."""

    carrier = inject_trace_headers(context=context)
    if "traceparent" not in carrier:
        return None
    encoded = json.dumps(carrier, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_INJECTED_CARRIER_BYTES:
        return None
    return encoded


def _extract(carrier: CarrierT, logger: logging.Logger | None) -> Context:
    if not isinstance(carrier, Mapping) or not _valid_carrier(carrier):
        _warn(logger)
        return Context()
    context = _PROPAGATOR.extract(carrier)
    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid or not span_context.is_remote:
        _warn(logger)
        return Context()
    return context


def extract_trace_context(
    raw: str | None, *, logger: logging.Logger | None = None
) -> Context:
    """Decode Stream trace metadata, failing open without exposing its value."""

    if raw is None or raw == "":
        return Context()
    if len(raw.encode("utf-8", errors="replace")) > _MAX_CARRIER_BYTES:
        _warn(logger)
        return Context()
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        _warn(logger)
        return Context()
    if (
        not isinstance(decoded, dict)
        or not decoded
        or not set(decoded) <= _ALLOWED_KEYS
        or "traceparent" not in decoded
        or any(not isinstance(value, str) for value in decoded.values())
    ):
        _warn(logger)
        return Context()
    carrier = {str(key): str(value) for key, value in decoded.items()}
    return _extract(carrier, logger)


def extract_http_trace_context(
    headers: Mapping[str, str], *, logger: logging.Logger | None = None
) -> Context:
    """Extract only traceparent/tracestate from HTTP headers."""

    carrier: dict[str, str] = {}
    saw_trace_header = False
    for raw_key, raw_value in headers.items():
        key = str(raw_key).lower()
        if key not in _ALLOWED_KEYS:
            continue
        saw_trace_header = True
        if not isinstance(raw_value, str) or not raw_value:
            _warn(logger)
            return Context()
        carrier[key] = raw_value
    if not carrier:
        if saw_trace_header:
            _warn(logger)
        return Context()
    if len(json.dumps(carrier, separators=(",", ":")).encode()) > _MAX_CARRIER_BYTES:
        _warn(logger)
        return Context()
    return _extract(carrier, logger)
