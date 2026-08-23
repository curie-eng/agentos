"""OTel: the gen_ai span tree is emitted for a turn; exporter wiring is gated."""

import anyio
from aci_protocol import Event, OtelConfig
from curie_runner import RunTracer, SideEffectClassifier, build_tracer_provider
from curie_runner.fake import FakeModelSession
from curie_runner.otel import _SchemaValidatingSpanProcessor
from curie_runner.session import SessionRunner
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_run_emits_agent_generation_and_tool_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,  # default_turn: text + Bash tool + result usage
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"agent.run", "llm.generation", "execute_tool"} <= set(spans)
    assert spans["agent.run"].attributes["langfuse.trace.name"] == "curie-run:test"
    gen = spans["llm.generation"]
    assert gen.attributes["gen_ai.request.model"] == "fake-model"
    assert gen.attributes["gen_ai.usage.output_tokens"] == 8
    assert spans["execute_tool"].attributes["gen_ai.tool.name"] == "Bash"


def test_generation_model_backfilled_from_sdk_when_unconfigured() -> None:
    # CURIE_MODEL unset (model=None) must NOT leave the generation span
    # model-less: Langfuse would then ingest it as an untyped span and drop token
    # usage to zero. The runner backfills the model the SDK reports on its first
    # assistant message (the fake scripts model="fake-model"), so the span stays a
    # typed generation with usage intact.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        model=None,
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    gen = {s.name: s for s in exporter.get_finished_spans()}["llm.generation"]
    assert gen.attributes["gen_ai.request.model"] == "fake-model"
    # The usage counts only land on a model-bearing generation, so their presence
    # is the end-to-end proof the span was typed as a generation, not a bare span.
    assert gen.attributes["gen_ai.usage.output_tokens"] == 8


def test_run_stamps_langfuse_session_and_user_ids() -> None:
    # Langfuse maps langfuse.session.id -> Sessions and langfuse.user.id -> Users,
    # but only from the trace-root span (same as langfuse.trace.name). The session
    # id is stable per session; the user id is the inbound event's Slack user.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        session_id="agent-abc-thread-123",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U42", ts="1")):
            pass

    anyio.run(go)

    root = {s.name: s for s in exporter.get_finished_spans()}["agent.run"]
    assert root.attributes["langfuse.session.id"] == "agent-abc-thread-123"
    assert root.attributes["langfuse.user.id"] == "U42"


def test_run_omits_langfuse_user_id_when_event_user_empty() -> None:
    # A turn with no event user (eval runs etc.) omits the attribute rather than
    # stamping an empty value; the session id still lands.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        session_id="agent-abc-thread-123",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="", ts="1")):
            pass

    anyio.run(go)

    root = {s.name: s for s in exporter.get_finished_spans()}["agent.run"]
    assert "langfuse.user.id" not in root.attributes
    assert root.attributes["langfuse.session.id"] == "agent-abc-thread-123"


def test_run_stamps_approval_decision_when_resuming_a_resolved_approval() -> None:
    # ADR-0076 Stone 3 (#889): the authority-free CURIE_APPROVAL_DECISION fact
    # threaded from the worker lands on the root span, so an operator can see
    # the outcome of an approval gate from the trace.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        model="fake-model",
        approval_decision="rejected",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    root = {s.name: s for s in exporter.get_finished_spans()}["agent.run"]
    assert root.attributes["gen_ai.approval.decision"] == "rejected"


def test_run_omits_approval_decision_on_an_ordinary_turn() -> None:
    # No approval was resumed, so the attribute is absent rather than stamped
    # empty or None -- the ordinary-turn default posture.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="curie-run:test",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    root = {s.name: s for s in exporter.get_finished_spans()}["agent.run"]
    assert "gen_ai.approval.decision" not in root.attributes


def test_tracer_provider_none_without_endpoint() -> None:
    otel = OtelConfig()
    assert build_tracer_provider(otel, "s1") is None


def test_tracer_provider_built_with_endpoint() -> None:
    otel = OtelConfig(endpoint="http://localhost:24318")
    provider = build_tracer_provider(otel, "s1")
    assert isinstance(provider, TracerProvider)
    provider.shutdown()


def test_run_span_stamps_session_and_sandbox_ids_when_present() -> None:
    # Per-turn ids belong on agent.run, not on the process-wide resource.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RunTracer(provider)

    with tracer.run_span(
        trace_name="curie-run:test",
        model="fake-model",
        session_id="s1",
        sandbox_id="sandbox-abc",
    ):
        pass

    root = {span.name: span for span in exporter.get_finished_spans()}["agent.run"]
    assert root.attributes["curie.session_id"] == "s1"
    assert root.attributes["curie.sandbox_id"] == "sandbox-abc"


def test_run_span_omits_sandbox_id_when_absent_or_empty() -> None:
    # Absent (default) and empty-string sandbox ids are both omitted from the
    # turn span rather than stamped as an empty attribute value.
    for sandbox_id in (None, ""):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = RunTracer(provider)

        with tracer.run_span(
            trace_name="curie-run:test",
            model="fake-model",
            session_id="s1",
            sandbox_id=sandbox_id,
        ):
            pass

        root = {span.name: span for span in exporter.get_finished_spans()}["agent.run"]
        assert root.attributes["curie.session_id"] == "s1"
        assert "curie.sandbox_id" not in root.attributes


def test_resource_stamps_schema_version() -> None:
    # ADR-0076: every exported trace carries the closed schema's version so a
    # consumer can tell which attribute-key set it was produced under.
    otel = OtelConfig(endpoint="http://localhost:24318")
    provider = build_tracer_provider(otel, "s1")
    assert provider is not None
    assert provider.resource.attributes["schema.version"] == "v1"
    provider.shutdown()


def _validated_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    # The validator only strips attributes on export; wire it ahead of an
    # in-memory exporter so tests can assert on what actually got through.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(_SchemaValidatingSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_validator_strips_attribute_key_outside_the_closed_schema() -> None:
    # A call site bypassing _set() (e.g. a future span.set_attribute call) must
    # not reach the exporter with an unlisted key (ADR-0076 decision 3).
    provider, exporter = _validated_exporter()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("langfuse.trace.name", "ok")
        span.set_attribute("some.unlisted.key", "should not survive export")

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes["langfuse.trace.name"] == "ok"
    assert "some.unlisted.key" not in finished.attributes


def test_validator_strips_value_still_matching_an_unscrubbed_secret() -> None:
    # A value that reaches set_attribute without going through redact_span_attribute
    # (bypassing _set()) is caught at export time even though its key is allowed.
    provider, exporter = _validated_exporter()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("langfuse.trace.name", "sk-abcdefghijklmnopqrstuvwx")

    (finished,) = exporter.get_finished_spans()
    assert "langfuse.trace.name" not in finished.attributes


def test_validator_strips_sequence_value_hiding_an_unscrubbed_secret() -> None:
    # #935: the validator is framed as a type-agnostic backstop (ADR-0076 decision
    # 3), but it only inspected `str` values -- so a SEQUENCE-valued attribute on
    # an allowed key carried a secret straight to the exporter, past both the scrub
    # and this validator. OTel permits sequence values, so the backstop must
    # recurse rather than trust that no call site ever sets one.
    provider, exporter = _validated_exporter()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute(
            "langfuse.trace.name", ["sk-abcdefghijklmnopqrstuvwx", "clean"]
        )

    (finished,) = exporter.get_finished_spans()
    assert "langfuse.trace.name" not in finished.attributes


def test_validator_strips_clean_sequence_with_wrong_closed_schema_type() -> None:
    # A clean sequence is still invalid for a key whose closed type is `str`.
    provider, exporter = _validated_exporter()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("langfuse.trace.name", ["curie-run:test", "clean"])

    (finished,) = exporter.get_finished_spans()
    assert "langfuse.trace.name" not in finished.attributes


def test_validator_leaves_clean_allowed_attributes_untouched() -> None:
    provider, exporter = _validated_exporter()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("langfuse.trace.name", "curie-run:test")
        span.set_attribute("gen_ai.usage.input_tokens", 12)

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes["langfuse.trace.name"] == "curie-run:test"
    assert finished.attributes["gen_ai.usage.input_tokens"] == 12
