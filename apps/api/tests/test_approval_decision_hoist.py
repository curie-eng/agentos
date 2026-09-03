"""Unit tests for hoisting the resolved approval decision out of a Langfuse trace.

The runner stamps `gen_ai.approval.decision` as an OTel span attribute on the
root agent.run span (ADR-0076 Stone 3, #889) rather than a resource attribute,
but Langfuse's OTLP ingestion still may surface it on the trace's metadata or
only on the observations, so the hoist probes both -- mirroring
`hoist_sandbox_id` (see test_sandbox_hoist.py). These tests fake both payload
shapes; the real propagation is exercised only against a live Langfuse.
"""

from typing import Any

from curie_api.langfuse import hoist_approval_decision


def test_hoist_from_trace_metadata() -> None:
    trace = {"id": "t1", "metadata": {"gen_ai.approval.decision": "approved"}}
    assert hoist_approval_decision(trace, []) == "approved"


def test_hoist_from_trace_resource_attributes() -> None:
    trace = {
        "id": "t1",
        "resourceAttributes": {"attributes": {"gen_ai.approval.decision": "rejected"}},
    }
    assert hoist_approval_decision(trace, []) == "rejected"


def test_hoist_from_first_observation_when_trace_lacks_it() -> None:
    trace = {"id": "t1", "metadata": {"other": "x"}}
    observations: list[dict[str, Any]] = [
        {
            "id": "root",
            "type": "SPAN",
            "resourceAttributes": {"gen_ai.approval.decision": "expired"},
        },
        {"id": "child", "type": "SPAN"},
    ]
    assert hoist_approval_decision(trace, observations) == "expired"


def test_hoist_prefers_trace_over_observation() -> None:
    trace = {"id": "t1", "metadata": {"gen_ai.approval.decision": "approved"}}
    observations = [{"id": "root", "metadata": {"gen_ai.approval.decision": "rejected"}}]
    assert hoist_approval_decision(trace, observations) == "approved"


def test_hoist_from_later_resume_observation_in_correlated_trace() -> None:
    # The dispatcher-rooted trace includes spans before and after agent.run.
    # Observation ordering cannot decide whether the resume is visible.
    observations = [
        {"id": "reply", "name": "curie.reply.update"},
        {"id": "ingress", "metadata": {"gen_ai.approval.decision": ""}},
        {"id": "invalid", "metadata": {"gen_ai.approval.decision": False}},
        {"id": "resume", "metadata": {"gen_ai.approval.decision": "approved"}},
    ]
    assert hoist_approval_decision({"id": "t1"}, observations) == "approved"
    assert hoist_approval_decision({"id": "t1"}, observations[:-1]) is None


def test_hoist_returns_none_for_an_ordinary_turn() -> None:
    # No approval was resumed this turn -- the ordinary, most common case.
    trace = {"id": "t1", "metadata": {"other": "x"}}
    observations = [{"id": "root", "type": "SPAN"}]
    assert hoist_approval_decision(trace, observations) is None


def test_hoist_ignores_empty_value() -> None:
    trace = {"id": "t1", "metadata": {"gen_ai.approval.decision": ""}}
    assert hoist_approval_decision(trace, []) is None
