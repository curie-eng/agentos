"""Runner-specific gen_ai spans on the platform's shared OTEL runtime.

Each turn is an ``agent.run`` SERVER span carrying a ``langfuse.trace.name``,
with a child ``llm.generation`` span holding ``gen_ai.request.model`` and
``gen_ai.usage.*`` token counts, plus a child ``execute_tool`` span per tool
call. The HTTP server attaches only an incoming W3C Trace Context parent before
opening this tree, so ``agent.run`` is a descendant of the worker RPC when one
exists and a valid root when it does not.

Provider construction, correlated logs, batching, resource identity, redaction,
flush, and shutdown live in ``curie_telemetry``. This module deliberately keeps
only the runner's established Langfuse/gen-ai span shape and a compatibility
provider builder used by older callers and drift tests.

Per ADR-0076, the runner's shared-runtime and runner-owned attributes are mirrored
by the closed ``SpanAttributeKey`` enum below. Runner-specific setters use the
enum rather than bare strings; the shared bootstrap is governed by the same
owner-partitioned artifact. ``SCHEMA_VERSION`` changes only when a key is removed,
renamed, or retyped. ``SPAN_ATTRIBUTE_VALUE_TYPES`` mirrors each exact type for
the drift gate.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

from aci_protocol import OtelConfig
from curie_telemetry.attributes import SCHEMA_VERSION as SCHEMA_VERSION
from curie_telemetry.attributes import SchemaValidatingSpanProcessor
from curie_telemetry.bootstrap import service_resource
from curie_telemetry.redact import redact_value
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

_SERVICE_NAME = "curie-runner"
_SERVICE_VERSION = "0.0.0"


class SpanAttributeKey(StrEnum):
    """The closed shared-plus-runner telemetry key set.

    ADR-0076 decision 1. Str-mixin so a member is usable anywhere a plain
    attribute-value string is expected (e.g. dict keys, f-strings). Direct
    runner-owned setters pass a member here rather than a literal; process
    resource keys are emitted by the shared bootstrap from the same artifact.
    """

    TRACE_NAME = "langfuse.trace.name"
    SESSION_ID = "langfuse.session.id"
    USER_ID = "langfuse.user.id"
    # ADR-0076 Stone 3 (#889, epic #512): the resolved terminal decision
    # (approved/rejected/expired) of the approval a resume turn is resuming
    # from, threaded in from the worker's authority-free CURIE_APPROVAL_DECISION
    # boot-env fact. Closes the "did an approval get requested" gap ADR-0038
    # named open, on the existing span stream.
    APPROVAL_DECISION = "gen_ai.approval.decision"
    REQUEST_MODEL = "gen_ai.request.model"
    MODEL = "model"
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read_input_tokens"
    USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation_input_tokens"
    TOOL_NAME = "gen_ai.tool.name"
    OPERATION_NAME = "gen_ai.operation.name"
    DEPLOYMENT_ENVIRONMENT_NAME = "deployment.environment.name"
    ERROR_TYPE = "error.type"
    EXCEPTION_ESCAPED = "exception.escaped"
    EXCEPTION_TYPE = "exception.type"
    SERVICE_NAME = "service.name"
    SERVICE_INSTANCE_ID = "service.instance.id"
    SERVICE_NAMESPACE = "service.namespace"
    SERVICE_VERSION = "service.version"
    CURIE_SESSION_ID = "curie.session_id"
    CURIE_SANDBOX_ID = "curie.sandbox_id"
    SCHEMA_VERSION_KEY = "schema.version"


# ADR-0076 decision 2: a value-type change to an existing key is a breaking,
# version-bump-worthy change exactly like a remove or rename, so it needs its
# own source of truth to diff against -- the type half of the closed schema,
# parallel to ``SpanAttributeKey`` being the key half. Every member above must
# appear here exactly once, mapped to its value-type name ("str", "int", or
# deliberate "bool"); the ``gen_ai.usage.*`` token counts are the int members.
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
    SpanAttributeKey.DEPLOYMENT_ENVIRONMENT_NAME: "str",
    SpanAttributeKey.ERROR_TYPE: "str",
    SpanAttributeKey.EXCEPTION_ESCAPED: "bool",
    SpanAttributeKey.EXCEPTION_TYPE: "str",
    SpanAttributeKey.SERVICE_NAME: "str",
    SpanAttributeKey.SERVICE_INSTANCE_ID: "str",
    SpanAttributeKey.SERVICE_NAMESPACE: "str",
    SpanAttributeKey.SERVICE_VERSION: "str",
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

    span.set_attribute(key.value, redact_value(value))


class _SchemaValidatingSpanProcessor(SchemaValidatingSpanProcessor):
    """Compatibility wrapper for the runner-scoped shared export backstop."""

    def __init__(self) -> None:
        super().__init__(_SERVICE_NAME)


def build_tracer_provider(
    otel: OtelConfig, session_id: str, sandbox_id: str | None = None
) -> TracerProvider | None:
    """Compatibility provider builder; process bootstrap uses shared configure.

    The legacy arguments remain source-compatible, but per-turn identities no
    longer belong on process resources. ``RunTracer.run_span`` stamps them on
    ``agent.run`` instead.
    """

    if not otel.endpoint:
        return None

    _ = (session_id, sandbox_id)
    resource = service_resource(_SERVICE_NAME, _SERVICE_VERSION)
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    # The validator must run before the exporting processor (registration order)
    # so the exporter only ever sees attributes the closed schema allows.
    provider.add_span_processor(_SchemaValidatingSpanProcessor())
    # Compatibility callers still get bounded batching. The process bootstrap
    # uses curie_telemetry.configure() instead; this helper remains for the
    # exported legacy API and the ADR-0076 resource drift checks.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(),
            max_queue_size=2048,
            schedule_delay_millis=500,
            max_export_batch_size=512,
            export_timeout_millis=500,
        )
    )
    return provider


class RunTracer:
    """Thin wrapper over an OTel tracer emitting the runner's gen_ai span tree.

    A None provider yields a no-op tracer so callers need no branching.
    """

    def __init__(self, provider: TracerProvider | Tracer | None) -> None:
        if isinstance(provider, TracerProvider):
            self._provider: TracerProvider | None = provider
            self._tracer = provider.get_tracer("curie-runner")
        elif provider is not None:
            self._provider = None
            self._tracer = provider
        else:
            self._provider = None
            self._tracer = trace.get_tracer("curie-runner")

    @contextmanager
    def run_span(
        self,
        trace_name: str,
        model: str | None,
        session_id: str | None = None,
        user_id: str | None = None,
        approval_decision: str | None = None,
        sandbox_id: str | None = None,
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

        with self._tracer.start_as_current_span(
            "agent.run",
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as root:
            _set(root, SpanAttributeKey.TRACE_NAME, trace_name)
            _set(root, SpanAttributeKey.SCHEMA_VERSION_KEY, SCHEMA_VERSION)
            if session_id:
                _set(root, SpanAttributeKey.SESSION_ID, session_id)
                _set(root, SpanAttributeKey.CURIE_SESSION_ID, session_id)
            if sandbox_id:
                _set(root, SpanAttributeKey.CURIE_SANDBOX_ID, sandbox_id)
            if user_id:
                _set(root, SpanAttributeKey.USER_ID, user_id)
            if approval_decision:
                _set(root, SpanAttributeKey.APPROVAL_DECISION, approval_decision)
            with self._tracer.start_as_current_span(
                "llm.generation",
                record_exception=False,
                set_status_on_exception=False,
            ) as gen:
                span = _GenerationSpan(self._tracer, root, gen)
                # Stamp the configured model at span open when CURIE_MODEL is
                # set; otherwise the span stays model-less until the SDK reports
                # the actual model on its first assistant message (record_model).
                span.record_model(model)
                try:
                    yield span
                except BaseException as exc:
                    exception_type = type(exc).__name__
                    span.record_outcome(
                        "abandoned",
                        error_type=exception_type,
                        exception_type=exception_type,
                        exception_escaped=True,
                    )
                    raise
                finally:
                    span.ensure_outcome()

    def shutdown(self) -> None:
        """Flush and shut down the exporter if one was configured."""

        if self._provider is not None:
            self._provider.shutdown()


class _GenerationSpan:
    """Handle for annotating the generation span and emitting tool child spans."""

    def __init__(self, tracer: Tracer, root: Any, span: Any) -> None:
        self._tracer = tracer
        self._root = root
        self._span = span
        self._model_recorded = False
        self._outcome_recorded = False

    def record_outcome(
        self,
        status: object,
        *,
        error_type: str | None = None,
        exception_type: str | None = None,
        exception_escaped: bool | None = None,
    ) -> None:
        """Set a terminal status and value-free exception classification."""

        if self._outcome_recorded:
            return
        value = getattr(status, "value", status)
        if value in {"done", "idle-awaiting-input", "awaiting-approval"}:
            self._root.set_status(Status(StatusCode.OK))
        else:
            self._root.set_status(Status(StatusCode.ERROR))
            _set(
                self._root,
                SpanAttributeKey.ERROR_TYPE,
                error_type or str(value),
            )
            if exception_type is not None:
                _set(
                    self._root,
                    SpanAttributeKey.EXCEPTION_TYPE,
                    exception_type,
                )
            if exception_escaped is not None:
                _set(
                    self._root,
                    SpanAttributeKey.EXCEPTION_ESCAPED,
                    exception_escaped,
                )
        self._outcome_recorded = True

    def ensure_outcome(self) -> None:
        """Treat an unclassified/abandoned span as an explicit error."""

        if not self._outcome_recorded:
            self.record_outcome("abandoned")

    @contextmanager
    def lifecycle_log_context(self) -> Iterator[None]:
        """Correlate terminal lifecycle logs to ``agent.run`` itself."""

        with trace.use_span(self._root, end_on_exit=False):
            yield

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
