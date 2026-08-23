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

On the write side, observability is swapped at the OTLP wire, not in code. The API,
dispatcher, worker, and runner export spans and correlated logs to one collector; the
collector is the only component that authenticates and forwards to a backend. Services
never speak the backend directly. Swapping the store means repointing one collector
exporter block, not changing a service. The opinionated core is the causal turn tree, the
runner's `agent.run` → `llm.generation` → `execute_tool` subtree, and the closed,
versioned attributes and events exported by each service.

## Current contract

A second backend must ingest the standard OTLP trace and log exports created by
`configure` (`packages/telemetry/src/curie_telemetry/bootstrap.py::configure`). The
contract is:

- **Four emitters, one bootstrap.** The API, dispatcher, worker, and runner configure
  `curie-api`, `curie-dispatcher`, `curie-worker`, and `curie-runner` runtimes
  respectively. Each runtime owns explicit trace and log providers; it does not install a
  process-global provider or replace stderr logging.
- **Standard environment, including an honest off switch.** Signal-specific
  `OTEL_EXPORTER_OTLP_TRACES_*` / `OTEL_EXPORTER_OTLP_LOGS_*` values override the generic
  `OTEL_EXPORTER_OTLP_*` endpoint, protocol, and headers. HTTP/protobuf and gRPC are both
  accepted on the service-to-collector hop. When neither signal has an endpoint (or
  `OTEL_SDK_DISABLED=true`), `configure` returns a true no-op runtime: no provider,
  exporter, queue, handler, or network attempt is created.
- **Resources identify processes; spans identify turns.** `service_resource`
  (`packages/telemetry/src/curie_telemetry/bootstrap.py::service_resource`) emits only
  process facts such as `service.name`, namespace, version, instance id,
  `deployment.environment.name`, and `schema.version`. Agent, session, user, and sandbox
  identity is attached only after the worker resolves the binding, then carried on the
  relevant worker and runner spans. A process resource never freezes one turn's identity
  onto later turns.
- **The vocabulary and privacy boundary are closed per service.** The shared schema in
  `attributes.py` (`packages/telemetry/src/curie_telemetry/attributes.py`) partitions
  allowed attributes and event names by emitter. `SchemaValidatingSpanProcessor`
  (`packages/telemetry/src/curie_telemetry/attributes.py::SchemaValidatingSpanProcessor`)
  drops unknown, mistyped, or recursively leaking attributes and unlisted events before
  export. `SchemaValidatingLogRecordProcessor`
  (`packages/telemetry/src/curie_telemetry/attributes.py::SchemaValidatingLogRecordProcessor`)
  applies the same closed/redacted policy to logs. Prompts, tool content, secrets, and
  transport identifiers are not telemetry attributes.
- **Export is bounded and lifecycle-owned.** Both signals use bounded batch queues and
  export timeouts. `TelemetryRuntime.force_flush`
  (`packages/telemetry/src/curie_telemetry/bootstrap.py::TelemetryRuntime.force_flush`)
  and `TelemetryRuntime.shutdown`
  (`packages/telemetry/src/curie_telemetry/bootstrap.py::TelemetryRuntime.shutdown`)
  share a hard two-second ceiling and detach only the handlers that runtime installed.
- **Trace Context crosses transports, not domain models.** Dispatcher and API producer
  spans inject only a W3C `traceparent`/optional `tracestate` carrier into Valkey Stream
  metadata beside the unchanged `QueuedTurn` payload. The worker consumer extracts it,
  and `RunnerClient._request`
  (`apps/worker/src/curie_worker/runner_client.py::RunnerClient._request`) injects the
  current context into the worker-to-runner HTTP headers. Missing metadata deliberately
  starts a valid worker root; malformed metadata is ignored with a value-free warning and
  a `trace_context.invalid` event rather than rejecting the turn.
- **The causal tree is observable at its real ownership boundaries.** A dispatcher or API
  `send curie:runs` producer parents the worker's `process curie:runs` consumer. Worker
  routing, sandbox claim/start/stop, and the runner HTTP client are descendants; the
  runner accepts that HTTP parent and opens its `agent.run` server span. Reply completion
  and broker settlement remain events on the worker side. Static, value-free lifecycle
  records emitted while those spans are current become trace/span-correlated OTLP logs;
  stderr diagnostics remain available independently.
- **The runner keeps its established GenAI subtree.** `SpanAttributeKey`
  (`runner/src/curie_runner/otel.py::SpanAttributeKey`) and
  `SPAN_ATTRIBUTE_VALUE_TYPES`
  (`runner/src/curie_runner/otel.py::SPAN_ATTRIBUTE_VALUE_TYPES`) mirror the shared plus
  runner-owned key set for the ADR-0076 drift gate. `RunTracer.run_span`
  (`runner/src/curie_runner/otel.py::RunTracer.run_span`) opens `agent.run` and
  `llm.generation`; model, token-usage, approval-decision, and `execute_tool` children
  retain their existing semantics.
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

One backend: Langfuse, reached through the OTel Collector (which authenticates and
forwards because Langfuse OTLP ingest is HTTP-only). Four services emit into that wire,
but none knows which backend sits behind it. This foundation adds no query model,
installable backend, agent-discovery surface, token/cost semantics, or console log-tail
backend. The existing API Langfuse proxy and UI read path remain separate and unchanged.

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
