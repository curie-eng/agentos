"""W3C trace context transport metadata remains bounded and fail open."""

from __future__ import annotations

import json
import logging

import pytest
from curie_telemetry.carrier import (
    TRACE_CONTEXT_FIELD,
    extract_trace_context,
    inject_trace_context,
)
from opentelemetry import baggage, trace
from opentelemetry.context import attach, detach
from opentelemetry.sdk.trace import TracerProvider

TRACE_ID = "11111111111111111111111111111111"
PARENT_ID = "2222222222222222"


def _is_valid(context: object) -> bool:
    return trace.get_current_span(context).get_span_context().is_valid  # type: ignore[arg-type]


def test_carrier_field_is_transport_metadata_not_a_payload_key() -> None:
    assert TRACE_CONTEXT_FIELD == "trace_context"


def test_inject_emits_only_w3c_trace_headers_and_never_baggage() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("carrier-test")
    baggage_context = baggage.set_baggage("prompt", "do not transport this")
    token = attach(baggage_context)
    try:
        with tracer.start_as_current_span("producer") as span:
            raw = inject_trace_context()
            assert raw is not None
            carrier = json.loads(raw)
            span_context = span.get_span_context()
    finally:
        detach(token)
        provider.shutdown()

    assert set(carrier) <= {"traceparent", "tracestate"}
    assert "baggage" not in carrier
    assert "do not transport this" not in raw
    assert carrier["traceparent"].startswith(
        f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-"
    )
    assert len(raw.encode()) <= 512


def test_inject_without_a_valid_current_span_omits_the_optional_carrier() -> None:
    assert inject_trace_context() is None


@pytest.mark.parametrize("raw", [None, ""])
def test_missing_carrier_returns_an_empty_context_without_a_warning(
    raw: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("carrier-missing")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        context = extract_trace_context(raw, logger=logger)
    assert not _is_valid(context)
    assert caplog.records == []


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        '{"traceparent":"00-bad"}',
        json.dumps(
            {
                "traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01",
                "baggage": "prompt=never",
            }
        ),
        "x" * 4097,
    ],
)
def test_malformed_oversized_or_extra_key_carriers_are_ignored_value_free(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("carrier-malformed")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        context = extract_trace_context(raw, logger=logger)

    assert not _is_valid(context)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ignored malformed trace context" in logged
    assert raw not in logged
    assert TRACE_ID not in logged
    assert "prompt=never" not in logged


def test_valid_carrier_extracts_the_remote_parent() -> None:
    raw = json.dumps({"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"})
    context = extract_trace_context(raw, logger=logging.getLogger("carrier-valid"))
    span_context = trace.get_current_span(context).get_span_context()

    assert span_context.is_valid
    assert span_context.is_remote
    assert f"{span_context.trace_id:032x}" == TRACE_ID
    assert f"{span_context.span_id:016x}" == PARENT_ID
