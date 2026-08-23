"""Worker-wide test helpers for observing OpenTelemetry output in memory.

The recorder deliberately owns a private ``TracerProvider`` per test instead of
installing one globally.  Worker components accept the runtime's tracer as an
optional dependency, so telemetry tests can observe finished spans without
changing the provider seen by unrelated tests (especially the no-endpoint
bootstrap controls).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer


@dataclass(frozen=True)
class SpanRecorder:
    """A private tracer and deterministic view of its completed spans."""

    tracer: Tracer
    exporter: InMemorySpanExporter

    def spans(
        self,
        *,
        name: str | None = None,
        kind: SpanKind | None = None,
    ) -> list[ReadableSpan]:
        spans = list(self.exporter.get_finished_spans())
        if name is not None:
            spans = [span for span in spans if span.name == name]
        if kind is not None:
            spans = [span for span in spans if span.kind is kind]
        return spans

    def one(self, name: str) -> ReadableSpan:
        matches = self.spans(name=name)
        assert len(matches) == 1, f"expected one {name!r} span, got {matches}"
        return matches[0]

    def clear(self) -> None:
        self.exporter.clear()


@pytest.fixture
def span_recorder() -> SpanRecorder:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = SpanRecorder(provider.get_tracer("curie-worker-tests"), exporter)
    try:
        yield recorder
    finally:
        provider.shutdown()
