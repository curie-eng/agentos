---
seam: Telemetry / OTEL
kind: SOFT
impls: 1
grade: B+
vision_row: Observability
epics:
  - "#47"
order: 7
---
# INTERFACE: Telemetry / OTEL

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** B+
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

On the write side, observability is swapped at the OTLP wire, not in code: the runner
exports `gen_ai.*` spans over OTLP-HTTP to a collector, and the collector is the only
component that authenticates and forwards to a backend. Services never speak the
backend directly. Swapping the trace store means repointing one collector exporter
block, not touching the runner. The opinionated core is the span tree shape
(`agent.run` → `llm.generation` → `execute_tool`) and the attributes on it, which since
ADR-0076 are a closed, versioned key set rather than an open bag of `gen_ai.*` names.

## Current contract

A second backend must ingest the OTLP-HTTP export produced in
`runner/src/curie_runner/otel.py`. Per ADR-0076 that export is a closed, versioned
attribute schema, not an open bag of `gen_ai.*` keys:

- **The vocabulary is closed and committed.** `SpanAttributeKey`
  (`packages/telemetry-schema/src/curie_telemetry_schema/__init__.py::SpanAttributeKey`) is the only key set the runner
  may attach: `langfuse.trace.name`, `langfuse.session.id`, `langfuse.user.id`,
  `gen_ai.approval.decision`, `gen_ai.request.model`, a bare `model`,
  `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read_input_tokens` /
  `cache_creation_input_tokens`, `gen_ai.tool.name`, `gen_ai.operation.name`, plus the
  resource keys `service.name`, `curie.session_id`, `curie.sandbox_id` and
  `schema.version`. `SPAN_ATTRIBUTE_VALUE_TYPES`
  (`runner/src/curie_runner/otel.py::SPAN_ATTRIBUTE_VALUE_TYPES`) declares each key's
  value type (the four usage counts are `int`, every other key is `str`).
- **The schema is versioned.** `SCHEMA_VERSION`
  (`runner/src/curie_runner/otel.py::SCHEMA_VERSION`) is `v1`, stamped on the resource as
  `schema.version`, and bumps only when a key is removed, renamed, or retyped; a new
  optional key is additive. `runner/schema/otel-attributes.schema.json` is the committed
  mirror, and `runner/tests/test_otel_schema_drift.py` fails CI when the mirror, the enum,
  the declared types, or a real run's emitted attributes disagree.
- `build_tracer_provider` (`runner/src/curie_runner/otel.py::build_tracer_provider`) takes
  `(otel, session_id, sandbox_id=None)` and returns `None` when `otel.endpoint` is unset
  (so offline runs neither export nor fail). Otherwise the `TracerProvider`'s `Resource`
  carries `service.name` (`curie-runner`), `curie.session_id`, `schema.version`, and
  `curie.sandbox_id` when that value is non-empty.
- **A fail-closed validator runs ahead of the exporter.** The provider registers
  `_SchemaValidatingSpanProcessor`
  (`runner/src/curie_runner/otel.py::_SchemaValidatingSpanProcessor`) first, then
  `SimpleSpanProcessor(OTLPSpanExporter())`. On each span ending the validator strips any
  attribute whose key falls outside the closed set, or whose value (or any element of a
  sequence value) still matches a redaction pattern after the `redact.py` scrub. It drops
  the offending attribute, never the span, so the exporter only ever sees schema-legal
  attributes.
- Endpoint/headers come from the standard `OTEL_EXPORTER_OTLP_*` env vars, read by the
  opentelemetry SDK itself because the exporter is constructed argument-free;
  `SessionConfig.otel` is the typed view of the same vars.
- `RunTracer.run_span` (`runner/src/curie_runner/otel.py::RunTracer.run_span`) takes
  `(trace_name, model, session_id=None, user_id=None, approval_decision=None)`, opens the
  root `agent.run` (`SpanKind.SERVER`) and a child `llm.generation` span. It always stamps
  `langfuse.trace.name` on the root, and stamps `langfuse.session.id`, `langfuse.user.id`
  and `gen_ai.approval.decision` only when the corresponding value is non-empty (the
  approval decision is present only on a turn resuming a resolved approval).
- On the generation span, `_GenerationSpan.record_model`
  (`runner/src/curie_runner/otel.py::_GenerationSpan.record_model`) stamps both
  `gen_ai.request.model` and the bare `model`, once, first non-empty value winning;
  `record_usage` (`runner/src/curie_runner/otel.py::_GenerationSpan.record_usage`) stamps
  up to four int attributes, the plain input/output counts plus the two prompt-cache
  counts, skipping any field the SDK usage mapping omits or reports as a non-int; and
  `tool_span` (`runner/src/curie_runner/otel.py::_GenerationSpan.tool_span`) emits an
  `execute_tool` child carrying `gen_ai.tool.name` and `gen_ai.operation.name`.

## Implementations today

One: Langfuse, reached through the OTel Collector (which authenticates and forwards,
since Langfuse OTLP ingest is HTTP-only). The runner does not know it is Langfuse — it
only knows OTLP. The runner is also the only span producer: neither the API nor the
worker builds a tracer, and the chart hands `OTEL_EXPORTER_OTLP_ENDPOINT` only to the
sandbox pod (`charts/curie/templates/agent-sandbox.yaml`). The read side (trace list, tree
reconstruction) is a separate concern in the API — Langfuse's query model spans several
API modules plus routers, not one isolated module — and is out of scope for this
write-side seam, though it is not attribute-blind (see leakage below).

## Known leakage

Three vendor-named attributes are set at the source on the root span rather than mapped in
the collector. `RunTracer.run_span` stamps `langfuse.trace.name`, `langfuse.session.id`,
and `langfuse.user.id` (`runner/src/curie_runner/otel.py::RunTracer.run_span`) so Langfuse
maps them to its name/Sessions/Users features. The collector pipeline is
receivers/`batch`/`otlphttp` with no attributes processor
(`charts/curie/templates/_helpers.tpl`), so nothing downstream renames them. A clean seam
would emit neutral attributes the collector maps to the vendor names; today all three
vendor names are set at the source, and they are members of the closed schema, so a second
backend inherits them.

A fourth, subtler one: `record_model` stamps the bare `model` key alongside the
semantic-convention `gen_ai.request.model`
(`runner/src/curie_runner/otel.py::_GenerationSpan.record_model`). `model` is not a
`gen_ai` semantic-convention key; it exists because the generation span has to carry a
model attribute the backend recognizes for the span to ingest as a generation rather than
an untyped span, and the Langfuse read-side integration test seeds both spellings
(`apps/api/tests/test_langfuse_integration.py`). A second backend inherits the duplicate.

The read side duplicates two of the keys as its own string literals rather than importing
the closed enum: `_SANDBOX_ATTR` (`apps/api/src/curie_api/langfuse.py::_SANDBOX_ATTR`) and
`_APPROVAL_DECISION_ATTR`
(`apps/api/src/curie_api/langfuse.py::_APPROVAL_DECISION_ATTR`), read by
`hoist_sandbox_id` and `hoist_approval_decision`
(`apps/api/src/curie_api/langfuse.py::hoist_approval_decision`). The drift gate covers
only the runner's enum against its committed mirror, so a rename on the producer side
passes CI while silently breaking these two readers. That is the concrete leak a second
implementation trips over: the schema is closed for the writer and open-coded for the
reader.

## Cross-links

- **Epic(s):** #47 — extends the observability / telemetry write path.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 2 (Observability / OTel store), grade B+.
- **ADR(s):** [ADR-0004](../../adr/0004-langfuse-observability-and-eval-backbone.md) — Langfuse as the single observability + eval backbone (OTLP over HTTP/protobuf to the collector).
  [ADR-0076](../../adr/0076-closed-typed-telemetry-attribute-schema.md) (Accepted, epic #512) — the closed, versioned attribute schema, the export-time validator, and the `gen_ai.approval.decision` key that closed the approval-gate observability gap ADR-0038 left open.
