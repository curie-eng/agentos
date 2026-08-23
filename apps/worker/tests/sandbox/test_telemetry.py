"""Observable sandbox lifecycle spans without changing substrate semantics."""

from __future__ import annotations

from dataclasses import replace

import pytest
from curie_worker.sandbox import (
    AffinityStore,
    CapacityExhaustedError,
    ClaimTimeoutError,
    QuotaRejection,
    RouteRecord,
    SandboxHandle,
    SandboxSubstrate,
    SubstrateConfig,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

from .conftest import FakeClaim, FakeSandbox, FakeSandboxClient


def _span_payload(span: ReadableSpan) -> str:
    return repr(
        (
            span.name,
            dict(span.attributes or {}),
            [
                (event.name, dict(event.attributes or {}))
                for event in span.events
            ],
            span.status.description,
        )
    )


def _outcome(span: ReadableSpan) -> str:
    assert span.attributes is not None
    value = span.attributes.get("curie.sandbox.outcome")
    assert isinstance(value, str)
    return value


def test_claim_reuse_suspend_resume_release_emit_closed_lifecycle_spans(
    fake_k8s: FakeSandboxClient,
    affinity: AffinityStore,
    config: SubstrateConfig,
    span_recorder,
) -> None:
    substrate = SandboxSubstrate(
        fake_k8s,
        affinity,
        config,
        tracer=span_recorder.tracer,
    )
    history_ref = "PRIVATE-HISTORY-REFERENCE-MUST-NOT-BE-EXPORTED"

    first = substrate.claim("T-OTEL-LIFECYCLE")
    assert substrate.claim("T-OTEL-LIFECYCLE") == first
    substrate.suspend("T-OTEL-LIFECYCLE", history_ref=history_ref)
    resumed = substrate.resume("T-OTEL-LIFECYCLE")
    assert resumed.claim_name != first.claim_name
    assert resumed.session_id == first.session_id
    assert resumed.history_ref == history_ref
    assert substrate.release("T-OTEL-LIFECYCLE") is True
    assert affinity.get("T-OTEL-LIFECYCLE") is None

    lifecycle = [
        span
        for span in span_recorder.spans()
        if span.name
        in {"sandbox.claim", "sandbox.suspend", "sandbox.resume", "sandbox.release"}
    ]
    assert lifecycle
    assert all(span.kind is SpanKind.INTERNAL for span in lifecycle)
    assert all(span.status.status_code is StatusCode.OK for span in lifecycle)
    claim_outcomes = {
        _outcome(span)
        for span in lifecycle
        if span.name == "sandbox.claim"
    }
    assert {"created", "reused"} <= claim_outcomes
    assert any(
        span.name == "sandbox.suspend" and _outcome(span) == "suspended"
        for span in lifecycle
    )
    assert any(
        span.name == "sandbox.resume" and _outcome(span) == "resumed"
        for span in lifecycle
    )
    assert any(
        span.name == "sandbox.release" and _outcome(span) == "released"
        for span in lifecycle
    )
    assert all(history_ref not in _span_payload(span) for span in lifecycle)


def test_lost_claim_race_reports_adopted_without_leaking_or_changing_winner(
    fake_k8s: FakeSandboxClient,
    affinity: AffinityStore,
    config: SubstrateConfig,
    span_recorder,
) -> None:
    substrate = SandboxSubstrate(
        fake_k8s,
        affinity,
        config,
        tracer=span_recorder.tracer,
    )
    winner = SandboxHandle(
        thread_key="T-ADOPT",
        claim_name="claim-winner-example",
        sandbox_name="sandbox-winner-example",
        namespace="test-ns",
        service_fqdn="sandbox-winner-example.test-ns.svc.cluster.local",
        port=8080,
        session_id="session-winner-example",
    )
    fake_k8s.claims[winner.claim_name] = FakeClaim(
        name=winner.claim_name,
        env={},
        labels={},
        sandbox_name=winner.sandbox_name,
    )
    fake_k8s.sandboxes[winner.sandbox_name] = FakeSandbox(
        name=winner.sandbox_name,
        service_fqdn=winner.service_fqdn,
    )
    real_create = fake_k8s.create_claim

    def create_then_lose(name: str, **kwargs: object) -> None:
        real_create(name, **kwargs)  # type: ignore[arg-type]
        affinity.put_if_absent("T-ADOPT", RouteRecord(handle=winner), ttl_seconds=60)

    fake_k8s.create_claim = create_then_lose  # type: ignore[method-assign]

    adopted = substrate.claim("T-ADOPT")

    assert adopted == winner
    assert winner.claim_name not in fake_k8s.deleted
    assert len(fake_k8s.deleted) == 1
    claim = span_recorder.one("sandbox.claim")
    assert claim.status.status_code is StatusCode.OK
    assert _outcome(claim) == "adopted"


def test_quota_and_timeout_claim_failures_have_error_outcomes_without_details(
    fake_k8s: FakeSandboxClient,
    affinity: AffinityStore,
    config: SubstrateConfig,
    span_recorder,
) -> None:
    rejection = QuotaRejection(
        quota_name="quota-example-private",
        resource="limits.cpu",
        requested="1",
        used="8",
        hard="8",
    )
    fake_k8s.quota_rejection = rejection
    short = replace(config, claim_timeout_seconds=0.02)
    substrate = SandboxSubstrate(
        fake_k8s,
        affinity,
        short,
        tracer=span_recorder.tracer,
    )

    with pytest.raises(CapacityExhaustedError):
        substrate.claim("T-CAPACITY")

    capacity = span_recorder.one("sandbox.claim")
    assert capacity.status.status_code is StatusCode.ERROR
    assert _outcome(capacity) == "capacity"
    assert "quota-example-private" not in _span_payload(capacity)
    assert "limits.cpu" not in _span_payload(capacity)

    span_recorder.clear()
    fake_k8s.quota_rejection = None
    fake_k8s.bind_ready = False
    fake_k8s.ready_reason = "PRIVATE-READY-REASON"
    fake_k8s.ready_message = "PRIVATE-READY-MESSAGE"
    timeout_substrate = SandboxSubstrate(
        fake_k8s,
        affinity,
        short,
        tracer=span_recorder.tracer,
    )

    with pytest.raises(ClaimTimeoutError):
        timeout_substrate.claim("T-TIMEOUT")

    timeout = span_recorder.one("sandbox.claim")
    assert timeout.status.status_code is StatusCode.ERROR
    assert _outcome(timeout) == "timeout"
    assert "PRIVATE-READY-REASON" not in _span_payload(timeout)
    assert "PRIVATE-READY-MESSAGE" not in _span_payload(timeout)
