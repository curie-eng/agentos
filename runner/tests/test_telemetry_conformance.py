"""Runner-owned telemetry conformance, separate from the frozen ACI suite."""

from collections import Counter

import anyio
from aci_protocol import Event
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from curie_runner import RunTracer, SideEffectClassifier
from curie_runner.fake import FakeModelSession
from curie_runner.otel import SPAN_ATTRIBUTE_VALUE_TYPES
from curie_runner.session import SessionRunner
from curie_telemetry_schema import SpanAttributeKey
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _violations(spans: list[ReadableSpan]) -> list[str]:
    errors: list[str] = []
    counts = Counter(span.name for span in spans)
    if counts["agent.run"] != 1:
        errors.append("expected one agent.run")
        return errors
    if counts["llm.generation"] != 2:
        errors.append("expected two true generations")
    if counts["execute_tool"] != 1:
        errors.append("expected one tool interval")

    root = next(span for span in spans if span.name == "agent.run")
    allowed = {member.value for member in SpanAttributeKey}
    declared_types = {
        member.value: SPAN_ATTRIBUTE_VALUE_TYPES[member] for member in SpanAttributeKey
    }
    for span in spans:
        for key, value in (span.attributes or {}).items():
            if key not in allowed:
                errors.append(f"forbidden attribute {key}")
            elif type(value).__name__ != declared_types[key]:
                errors.append(f"wrong type for {key}")
        if span.name != "agent.run":
            if root.context is None or span.parent is None:
                errors.append(f"{span.name} has no agent.run parent")
            elif span.parent.span_id != root.context.span_id:
                errors.append(f"{span.name} is not an agent.run sibling")
        if span.name == "execute_tool":
            if span.start_time is None or span.end_time is None or span.end_time <= span.start_time:
                errors.append("tool interval has zero duration")
            if span.attributes.get("curie.tool.outcome") not in {
                "success",
                "error",
                "cancelled",
            }:
                errors.append("tool outcome is not closed")
    return errors


def _canonical_spans() -> list[ReadableSpan]:
    call_id = "internal-conformance-call-PLACEHOLDER"
    script = [
        AssistantMessage(
            content=[TextBlock(text="checking")],
            model="observed-model",
            usage={"input_tokens": 2, "output_tokens": 3},
        ),
        AssistantMessage(
            content=[ToolUseBlock(id=call_id, name="Read", input={"path": "private"})],
            model="observed-model",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id=call_id, content="private result")]
        ),
        AssistantMessage(
            content=[TextBlock(text="done")],
            model="observed-model",
            usage={"input_tokens": 5, "output_tokens": 7},
        ),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="private-sdk-session-PLACEHOLDER",
            result="done",
            usage={"input_tokens": 99, "output_tokens": 99},
        ),
    ]
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runner = SessionRunner(
        session_factory=lambda: FakeModelSession(lambda: script),
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="conformance",
        model="configured-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(
            Event(type="message", text="go", user="U0EXAMPLE1", ts="1")
        ):
            pass
        await runner.close()

    anyio.run(go)
    return list(exporter.get_finished_spans())


def test_runner_telemetry_conforms_without_payloads_or_provider_ids() -> None:
    spans = _canonical_spans()
    assert not _violations(spans)
    material = repr([(span.name, span.attributes, span.events) for span in spans])
    assert "internal-conformance-call-PLACEHOLDER" not in material
    assert "private-sdk-session-PLACEHOLDER" not in material
    assert "private result" not in material


def test_conformance_rejects_legacy_monolithic_generation_and_zero_duration_tool() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("legacy")
    with tracer.start_as_current_span("agent.run"):
        with tracer.start_as_current_span("llm.generation"):
            tool = tracer.start_span("execute_tool", start_time=100)
            tool.end(end_time=100)

    violations = _violations(list(exporter.get_finished_spans()))
    assert "expected two true generations" in violations
    assert "execute_tool is not an agent.run sibling" in violations
    assert "tool interval has zero duration" in violations


def test_conformance_rejects_payload_argument_result_and_raw_id_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("forbidden")
    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("gen_ai.prompt", "private content")
        span.set_attribute("gen_ai.tool.arguments", "private args")
        span.set_attribute("gen_ai.tool.result", "private result")
        span.set_attribute("gen_ai.tool.call.id", "provider-id")

    violations = _violations(list(exporter.get_finished_spans()))
    assert {error for error in violations if error.startswith("forbidden attribute")} == {
        "forbidden attribute gen_ai.prompt",
        "forbidden attribute gen_ai.tool.arguments",
        "forbidden attribute gen_ai.tool.call.id",
        "forbidden attribute gen_ai.tool.result",
    }
