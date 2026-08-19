"""OTel tracing for the runner: gen_ai spans exported OTLP-HTTP to the collector.

Productizes the PT-4/PT-E prototype span shape. Each turn is a root ``agent.run``
(SERVER) span carrying a ``langfuse.trace.name``, with a child ``llm.generation``
span holding ``gen_ai.request.model`` and ``gen_ai.usage.*`` token counts, plus a
child ``execute_tool`` span per tool call (``gen_ai.tool.name`` /
``gen_ai.operation.name``). Langfuse maps a model-bearing span to a generation and
nests tool spans as observations, so this reconstructs the tool-call tree (S1).

Traces go to the OTel Collector over OTLP-HTTP, never directly to Langfuse: the
collector is the adapter that authenticates and forwards (Langfuse OTLP ingest is
HTTP-only). Endpoint/headers come from the standard ``OTEL_EXPORTER_OTLP_*`` env
vars via ``SessionConfig.otel``; the exporter is constructed argument-free so the
opentelemetry SDK's own env parsing applies (it appends ``/v1/traces`` to a base
``OTEL_EXPORTER_OTLP_ENDPOINT``). When no endpoint is configured the tracer is a
no-op, so unit tests and offline runs neither export nor fail.

Per ADR-0076, every attribute this module attaches comes from the shared closed
``SpanAttributeKey`` enum rather than a bare string, so a future call site
with an unlisted key is a construction-time error, not a silent addition to the
wire shape. ``SCHEMA_VERSION`` is bumped only when a key is removed, renamed, or
changes value type; a new optional key is additive and does not bump it.
``SPAN_ATTRIBUTE_VALUE_TYPES`` declares each key's value type, the mirror the
drift gate diffs to catch a retype.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

from aci_protocol import OtelConfig
from curie_telemetry_schema import SpanAttributeKey as SpanAttributeKey
from opentelemetry import trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Tracer

from .redact import redact_span_attribute, redact_text

_SERVICE_NAME = "curie-runner"

# ADR-0076 decision 2: additive (a new optional key) does not bump this; removing,
# renaming, or retyping an existing key does.
SCHEMA_VERSION = "v1"


# ADR-0076 decision 2: a value-type change to an existing key is a breaking,
# version-bump-worthy change exactly like a remove or rename, so it needs its
# own source of truth to diff against -- the type half of the closed schema,
# parallel to ``SpanAttributeKey`` being the key half. Every member must
# appear here exactly once, mapped to its value-type name ("str" or "int");
# the ``gen_ai.usage.*`` token counts are the only "int" members (see
# ``record_usage`` below and ``redact.py``'s "only str and int attributes are
# set today").
SPAN_ATTRIBUTE_VALUE_TYPES: Mapping[SpanAttributeKey, str] = {
    SpanAttributeKey.TRACE_NAME: "str",
    SpanAttributeKey.SESSION_ID: "str",
    SpanAttributeKey.USER_ID: "str",
    SpanAttributeKey.APPROVAL_DECISION: "str",
    SpanAttributeKey.REQUEST_MODEL: "str",
    SpanAttributeKey.MODEL: "str",
    SpanAttributeKey.USAGE_INPUT_TOKENS: "int",
    SpanAttributeKey.USAGE_OUTPUT_TOKENS: "int",
    SpanAttributeKey.USAGE_CACHE_READ_INPUT_TOKENS: "int",
    SpanAttributeKey.USAGE_CACHE_CREATION_INPUT_TOKENS: "int",
    SpanAttributeKey.TOOL_NAME: "str",
    SpanAttributeKey.OPERATION_NAME: "str",
    SpanAttributeKey.SERVICE_NAME: "str",
    SpanAttributeKey.CURIE_SESSION_ID: "str",
    SpanAttributeKey.CURIE_SANDBOX_ID: "str",
    SpanAttributeKey.SCHEMA_VERSION_KEY: "str",
}


# The ``usage`` mapping's own field names (SDK wire shape) to the span attribute
# they stamp, so ``record_usage`` can iterate without a per-field literal.
_USAGE_ATTRIBUTE_KEYS: Mapping[str, SpanAttributeKey] = {
    "input_tokens": SpanAttributeKey.USAGE_INPUT_TOKENS,
    "output_tokens": SpanAttributeKey.USAGE_OUTPUT_TOKENS,
    "cache_read_input_tokens": SpanAttributeKey.USAGE_CACHE_READ_INPUT_TOKENS,
    "cache_creation_input_tokens": SpanAttributeKey.USAGE_CACHE_CREATION_INPUT_TOKENS,
}


def _set(span: Any, key: SpanAttributeKey, value: object) -> None:
    """Set a span attribute through the redaction pass.

    Every ``set_attribute`` in this module goes through here so a future attribute
    cannot bypass the scrub by being written directly (see ``redact.py``), and
    ``key`` is a closed ``SpanAttributeKey`` member so an unlisted key cannot be
    attached by construction (ADR-0076).
    """

    span.set_attribute(key.value, redact_span_attribute(value))


def _still_leaks(value: object) -> bool:
    """Whether an attribute value still carries an unscrubbed secret.

    Type-agnostic on purpose (#935). ADR-0076 decision 3 frames the export
    validator as a universal backstop, but it only inspected ``str``, so a
    sequence-valued attribute on an ALLOWED key slipped a secret past BOTH the
    scrub and this validator — the two layers failing together, which is exactly
    what defense in depth is supposed to prevent. ``str`` is itself a Sequence, so
    it is matched first; anything that is neither a str nor a list/tuple (int,
    float, bool) cannot carry a pattern match and is clean by construction.
    """

    if isinstance(value, str):
        return redact_text(value) != value
    if isinstance(value, (list, tuple)):
        return any(_still_leaks(item) for item in value)
    return False


class _SchemaValidatingSpanProcessor(SpanProcessor):
    """Fail-closed export-time backstop (ADR-0076 decision 3).

    ``_set()`` already gates every attribute this module attaches through the
    closed ``SpanAttributeKey`` enum and the ``redact.py`` scrub; this processor
    exists for the call site that bypasses both by calling ``span.set_attribute``
    directly. On each span ending, it strips (does not replace) any attribute
    whose key is outside the closed schema, or whose value — or any element of a
    sequence value (#935) — still matches
    an unscrubbed-secret pattern after the existing redaction pass — dropping the
    offending attribute rather than the whole span, so one bad key costs a single
    field of trace data rather than the whole record.

    Must be registered on the provider ahead of the exporting processor
    (``TracerProvider.add_span_processor`` invokes processors in registration
    order); it mutates the span's attributes in place so the exporter that runs
    after it sees the cleaned set.
    """

    _ALLOWED_KEYS = frozenset(member.value for member in SpanAttributeKey)

    def on_end(self, span: ReadableSpan) -> None:
        # ReadableSpan.attributes is a read-only MappingProxyType view; the
        # underlying BoundedAttributes (span._attributes) is the same object the
        # concrete Span held, flagged immutable at Span.end() (see the SDK's own
        # `self._attributes._immutable = True` in Span.end()). Toggling that
        # private flag to mutate here mirrors the SDK's own pattern. Always a
        # BoundedAttributes at runtime (Span.__init__ constructs it directly);
        # the cast narrows past the Mapping-typed private attribute.
        raw = span._attributes  # noqa: SLF001
        if raw is None:
            return
        attributes = cast(BoundedAttributes, raw)
        was_immutable = attributes._immutable  # noqa: SLF001
        attributes._immutable = False  # noqa: SLF001
        try:
            for key in list(attributes.keys()):
                value = attributes[key]
                still_leaks = _still_leaks(value)
                if key not in self._ALLOWED_KEYS or still_leaks:
                    del attributes[key]
        finally:
            attributes._immutable = was_immutable  # noqa: SLF001


def build_tracer_provider(
    otel: OtelConfig, session_id: str, sandbox_id: str | None = None
) -> TracerProvider | None:
    """Build a TracerProvider exporting to the collector, or None if unconfigured.

    ``session_id`` is attached as a resource attribute so traces are attributable
    to the sandbox session that produced them. ``sandbox_id`` (the ACI
    ``CURIE_SANDBOX_ID``) is stamped alongside it when present so a trace is
    attributable to the concrete sandbox that ran it; an absent or empty value is
    omitted rather than stamped as an empty string.
    """

    if not otel.endpoint:
        return None

    attributes: dict[str, str] = {
        SpanAttributeKey.SERVICE_NAME.value: _SERVICE_NAME,
        SpanAttributeKey.CURIE_SESSION_ID.value: session_id,
        SpanAttributeKey.SCHEMA_VERSION_KEY.value: SCHEMA_VERSION,
    }
    if sandbox_id:
        attributes[SpanAttributeKey.CURIE_SANDBOX_ID.value] = sandbox_id
    resource = Resource.create(attributes)
    provider = TracerProvider(resource=resource)
    # The validator must run before the exporting processor (registration order)
    # so the exporter only ever sees attributes the closed schema allows.
    provider.add_span_processor(_SchemaValidatingSpanProcessor())
    # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS / _PROTOCOL from
    # the environment itself; SessionConfig.otel is the typed view of the same
    # vars, so an argument-free exporter and the config agree by construction.
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    return provider


class RunTracer:
    """Thin wrapper over an OTel tracer emitting the runner's gen_ai span tree.

    A None provider yields a no-op tracer so callers need no branching.
    """

    def __init__(self, provider: TracerProvider | None) -> None:
        self._provider = provider
        self._tracer: Tracer = (
            provider.get_tracer("curie-runner")
            if provider is not None
            else trace.get_tracer("curie-runner")
        )

    @contextmanager
    def run_span(
        self,
        trace_name: str,
        model: str | None,
        session_id: str | None = None,
        user_id: str | None = None,
        approval_decision: str | None = None,
    ) -> Iterator[_GenerationSpan]:
        """Open the root ``agent.run`` span and its child ``llm.generation`` span.

        ``session_id`` (the ACI ``CURIE_SESSION_ID``, one Slack thread) and
        ``user_id`` (the inbound event's Slack user) are stamped on the root span
        so Langfuse maps them to its Sessions and Users features respectively.
        Langfuse reads these from the trace-root span, exactly as it does
        ``langfuse.trace.name``; an empty or absent value is omitted rather than
        stamped, so a turn with no event user (eval runs etc.) carries no user id.

        ``approval_decision`` (ADR-0076 Stone 3, #889) is the authority-free
        CURIE_APPROVAL_DECISION fact -- present only when this turn is
        resuming a resolved approval -- stamped unconditionally when given so
        an operator can see the outcome from the trace.
        """

        with self._tracer.start_as_current_span("agent.run", kind=SpanKind.SERVER) as root:
            _set(root, SpanAttributeKey.TRACE_NAME, trace_name)
            if session_id:
                _set(root, SpanAttributeKey.SESSION_ID, session_id)
            if user_id:
                _set(root, SpanAttributeKey.USER_ID, user_id)
            if approval_decision:
                _set(root, SpanAttributeKey.APPROVAL_DECISION, approval_decision)
            with self._tracer.start_as_current_span("llm.generation") as gen:
                span = _GenerationSpan(self._tracer, gen)
                # Stamp the configured model at span open when CURIE_MODEL is
                # set; otherwise the span stays model-less until the SDK reports
                # the actual model on its first assistant message (record_model).
                span.record_model(model)
                yield span

    def shutdown(self) -> None:
        """Flush and shut down the exporter if one was configured."""

        if self._provider is not None:
            self._provider.shutdown()


class _GenerationSpan:
    """Handle for annotating the generation span and emitting tool child spans."""

    def __init__(self, tracer: Tracer, span: Any) -> None:
        self._tracer = tracer
        self._span = span
        self._model_recorded = False

    def record_model(self, model: str | None) -> None:
        """Stamp the generation model attribute once, first non-empty value wins.

        Langfuse only maps ``llm.generation`` to a GENERATION observation (and so
        records the ``gen_ai.usage.*`` token counts) when the span carries a model
        attribute; a model-less span ingests as an untyped SPAN with zero usage.
        The configured ``CURIE_MODEL`` is stamped at span open when set; when it
        is unset the runner backfills the actual model the SDK reports on its first
        assistant message, so the generation is typed either way. Only genuinely
        unknown models leave the attribute absent.
        """

        if self._model_recorded or not model:
            return
        _set(self._span, SpanAttributeKey.REQUEST_MODEL, model)
        _set(self._span, SpanAttributeKey.MODEL, model)
        self._model_recorded = True

    def record_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Attach gen_ai token-usage attributes from an SDK usage mapping.

        Prompt-cache tokens (``cache_read_input_tokens`` /
        ``cache_creation_input_tokens``, the Anthropic wire shape preserved even
        through OpenRouter) are recorded alongside the plain input/output counts,
        so a warm thread's cache reuse is observable in the trace rather than
        silently folded away. This is the signal the prompt-cache smoke test
        asserts on: a translating gateway that silently breaks caching shows up
        here as a warm turn with zero cache-read tokens.
        """

        if not usage:
            return
        for usage_field, attribute_key in _USAGE_ATTRIBUTE_KEYS.items():
            value = usage.get(usage_field)
            if isinstance(value, int):
                _set(self._span, attribute_key, value)

    def tool_span(self, tool_name: str) -> None:
        """Emit a short ``execute_tool`` child span for one tool call."""

        with self._tracer.start_as_current_span("execute_tool") as tool:
            _set(tool, SpanAttributeKey.TOOL_NAME, tool_name)
            _set(tool, SpanAttributeKey.OPERATION_NAME, "execute_tool")
