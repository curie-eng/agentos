"""Source contracts for the local parity ladder's OTLP evidence (#1817/#1818).

The expensive proof remains ``CURIE_E2E_TIERS=local curie dev e2e-ladder``.
These fast tests keep that proof from silently collapsing back to "the turn
finished": the local rung must own a real OTLP Collector sink, query its three
exported signal files, and run both healthy and injected-failure controls.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LADDER_PATH = REPO_ROOT / "cli" / "scripts" / "e2e-ladder.sh"
SINK_CONFIG_PATH = REPO_ROOT / "cli" / "scripts" / "fixtures" / "otel-e2e-sink-config.yaml"
MCP_RECEIPT_FIXTURE = REPO_ROOT / "cli" / "scripts" / "fixtures" / "mcp-receipt"


def _function_body(source: str, name: str, next_marker: str) -> str:
    start_marker = f"{name}() {{"
    assert start_marker in source, f"{LADDER_PATH}: missing {name}"
    start = source.index(start_marker)
    end = source.index(next_marker, start)
    return source[start:end]


def _shell_function(source: str, name: str) -> str:
    """Return one top-level shell function without depending on its successor."""

    start_marker = f"{name}() {{"
    assert start_marker in source, f"{LADDER_PATH}: missing {name}"
    start = source.index(start_marker)
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def test_local_ladder_owns_a_real_queryable_otlp_sink() -> None:
    """A plain local rung must create, query, and remove its own OTLP sink."""

    source = LADDER_PATH.read_text()
    rung = _function_body(source, "rung_local", "# local-release mode:")
    trap = _function_body(source, "cleanup", "trap cleanup EXIT")

    required_calls = (
        "start_local_otel_sink",
        "assert_local_otel_healthy_turn",
        "case_local_otel_runner_failure",
    )
    missing = [call for call in required_calls if call not in rung]
    assert not missing, (
        "the local rung can pass without querying the three OTLP signals and "
        f"its healthy/failure controls; missing calls: {missing}"
    )
    assert "stop_local_otel_sink" in trap, (
        "the global trap must reap the disposable sink even when the rung fails"
    )

    # The sink has to exist before `local up`, because service startup telemetry
    # and the ingress root are part of the proof. The query belongs after the
    # real turn finalized, not beside static compose/config assertions.
    assert rung.index("start_local_otel_sink") < rung.index("local up)")
    assert rung.index("assert_local_otel_healthy_turn") > rung.index(
        'assert_finalized_reply "local"'
    )

    assert SINK_CONFIG_PATH.is_file(), (
        "the local proof must use a checked-in Collector sink config rather "
        "than infer export from the application Collector's configuration"
    )
    sink = yaml.safe_load(SINK_CONFIG_PATH.read_text())
    assert "otlp" in sink["receivers"]
    pipelines = sink["service"]["pipelines"]
    for signal in ("traces", "logs", "metrics"):
        assert pipelines[signal]["receivers"] == ["otlp"]
        exporters = pipelines[signal]["exporters"]
        assert any(name.startswith("file/") for name in exporters), (
            f"the {signal} sink pipeline must retain queryable records, got {exporters}"
        )


def test_local_sink_assertion_proves_causality_correlation_and_bounded_metrics() -> None:
    """Pin the observable claims, not merely the sink container's presence."""

    source = LADDER_PATH.read_text()
    healthy_turn = _function_body(
        source,
        "assert_local_otel_healthy_turn",
        "case_local_otel_runner_failure()",
    )
    query = _function_body(
        source,
        "local_otel_query",
        "assert_bounded_metric_attributes()",
    )

    # `local message` delegates its one-shot producer to the real dispatcher,
    # then crosses Valkey through worker and runner before completing the reply.
    # Do not let this lapse into an API-ingress-only proof: the qualifying trace
    # has to include the dispatcher, worker, runner, and reply completion; the
    # same post-baseline observation also requires API telemetry from the live
    # health/readiness path.
    causal_spans = {
        "curie.queue.enqueue",
        "curie.queue.process",
        "curie.turn.process",
        "curie.sandbox.claim",
        "curie.runner.rpc",
        "agent.run",
        "curie.reply.post",
    }
    missing_spans = sorted(span for span in causal_spans if span not in query)
    assert not missing_spans, f"local trace assertion omits spans: {missing_spans}"

    for service in ("curie-dispatcher", "curie-worker", "curie-runner"):
        assert service in query, (
            "the qualifying causal trace must retain the "
            f"{service} service boundary"
        )
    assert "has_reply" in query, (
        "the qualifying causal trace must complete a reply, not stop at runner execution"
    )

    for field in ("traceId", "spanId", "service.name"):
        assert field in query, f"OTLP logs are not proved correlated without checking {field}"
    for service in ("curie-api", "curie-dispatcher", "curie-worker", "curie-runner"):
        assert f'"{service}"' in query, (
            f"the post-turn platform proof must observe fresh telemetry from {service}"
        )
    assert "platform_services = {\"curie-api\"" in query
    assert "exercised == platform_services" in query, (
        "the healthy proof must require fresh spans from every platform service"
    )

    success_metrics = {
        "curie.turn.accepted",
        "curie.turn.completed",
        "curie.turn.duration",
        "curie.queue.message.age",
        "curie.sandbox.lifecycle",
        "curie.runner.rpc.result",
    }
    missing_metrics = sorted(name for name in success_metrics if name not in query)
    assert not missing_metrics, (
        f"local success flow omits required operational metric points: {missing_metrics}"
    )

    # Event/session/sandbox/user identifiers are allowed on traces and logs but
    # never on metric points. The runtime manifest test owns the complete schema;
    # this guards the independent E2E query from accidentally accepting them.
    assert "assert_bounded_metric_attributes" in healthy_turn
    for forbidden in (
        "event.id",
        "session.id",
        "sandbox.id",
        "user.id",
        "trace_id",
        "span_id",
    ):
        assert forbidden in query, (
            f"the sink query never rejects high-cardinality metric key {forbidden!r}"
        )

    # The prompt and a credential-shaped sentinel exercise the negative privacy
    # side of the same collected payload. A sink query that only finds positive
    # names cannot prove sensitive values stayed out.
    assert "assert_local_otel_redacted" in healthy_turn
    assert "OTEL_E2E_SECRET_SENTINEL" in source


def test_local_runner_failure_is_falsifiable_against_the_healthy_control() -> None:
    """A failing command is evidence only when its telemetry differs from healthy."""

    source = LADDER_PATH.read_text()
    failure = _function_body(
        source,
        "case_local_otel_runner_failure",
        "# Rung 1:",
    )
    failure += _function_body(
        source,
        "local_otel_query",
        "assert_bounded_metric_attributes()",
    )

    for required in (
        "inject_local_runner_failure",
        "assert_local_otel_failed_turn",
        "restore_local_runner_health",
        "ERROR",
        "classified_failure",
        "done_delta",
        "deliberately non-retryable",
    ):
        assert required in failure, (
            f"the local failure control does not prove {required!r} against healthy"
        )
    assert "assert_local_otel_healthy_turn" in failure, (
        "the injected failure must restore the runner and observe a subsequent "
        "healthy control, or a permanently broken path could satisfy the negative"
    )

    failed_assertion = failure.index('assert_local_otel_failed_turn "$failure_before"')
    restored = failure.index("\n    restore_local_runner_health\n")
    assert failed_assertion < restored, (
        "the failure's trace/log/metric evidence must be exported before runner "
        "restoration can discard it"
    )
    assert "same traceId" in failure, (
        "the failed turn must pair its ERROR log with the failed trace"
    )


def test_local_otel_controls_distinguish_a_live_sink_from_a_turn() -> None:
    """Keep the no-turn and failure baselines falsifiable."""

    source = LADDER_PATH.read_text()
    zero_export = _function_body(
        source,
        "assert_local_otel_zero_export_control",
        "assert_local_otel_no_turn_pipeline_live()",
    )
    no_turn = _function_body(
        source,
        "assert_local_otel_no_turn_pipeline_live",
        "assert_local_otel_redacted()",
    )
    query = _function_body(
        source,
        "local_otel_query",
        "assert_bounded_metric_attributes()",
    )
    for metric in (
        "otelcol_receiver_accepted_metric_points",
        "otelcol_exporter_sent_metric_points",
    ):
        assert metric in zero_export and metric in no_turn
    assert "local_otel_query no-turn" in no_turn
    assert "curie.turn.accepted" in query
    assert "curie.turn.completed" in query

    failure = _function_body(source, "case_local_otel_runner_failure", "# Rung 1:")
    settle = failure.index("wait_for_local_otel_metric_settle")
    baseline = failure.index('local_otel_write_snapshot "$failure_before"')
    assert settle < baseline, (
        "a prior healthy metric export must settle before the failure baseline"
    )
    helper = _function_body(
        source,
        "wait_for_local_otel_metric_settle",
        "local_otel_self_metric_value()",
    )
    assert "sleep 12" in helper, (
        "the failure baseline must outwait the 10-second metric export interval"
    )


def test_product_oracle_discovers_only_the_seed_trace_from_bounded_transport() -> None:
    """A background/newest trace must be unable to satisfy the turn oracle."""

    source = LADDER_PATH.read_text()
    assert "local observability runs --limit 100" not in source, (
        "a newest-runs page can select an unrelated background trace; exact discovery "
        "must start from the seeded stream entry"
    )

    discover = _shell_function(source, "discover_trace_id_for_seed")
    for required in (
        "stream_start",
        "stream_end",
        "marker",
        "XRANGE",
        "traceparent",
        "[0-9a-f]{32}",
        "umask 077",
    ):
        assert required in discover, (
            "exact discovery must inspect only the marker's bounded stream slice, "
            f"validate its adjacent W3C carrier, and keep raw fields private; missing {required!r}"
        )
    assert "printf '%s\\n' \"$payload\"" not in discover
    assert "printf '%s\\n' \"$traceparent\"" not in discover

    query = _shell_function(source, "query_exact_seed_trace")
    assert 'observability run "$trace_id"' in query
    assert "sanitize_exact_trace_read" in query
    assert "observability runs" not in query
    assert "printf '%s\\n' \"$out\"" not in query, (
        "raw Langfuse JSON may contain prompt, user, session, and deployment fields"
    )
    sanitizer = _shell_function(source, "sanitize_exact_trace_read")
    for allowed in (
        "trace_id",
        "service",
        "operation",
        "observation_count",
        "observation_type",
        "approval_decision",
    ):
        assert allowed in sanitizer, f"sanitized evidence omits safe field {allowed!r}"
    for private in ("input", "output", "session", "user", "headers"):
        assert private in sanitizer, (
            f"sanitizer must explicitly prevent raw {private!r} from reaching stdout"
        )


def test_ordinary_mcp_and_approval_seeds_have_independent_receipts() -> None:
    """An unexercised seed is seed-invalid before telemetry is interpreted."""

    source = LADDER_PATH.read_text()
    for name, required in {
        "seed_ordinary_turn": ("assert_finalized_reply", "ordinary", "seed-invalid"),
        "seed_mcp_read_turn": ("mcp_receipt_call_count", "seed-invalid"),
        "seed_approval_resume_turn": (
            "awaiting-approval",
            "pending",
            "resolve",
            "assert_finalized_reply",
            "seed-invalid",
        ),
    }.items():
        body = _shell_function(source, name)
        missing = [marker for marker in required if marker not in body]
        assert not missing, f"{name} lacks independent outcome evidence: {missing}"
        assert "discover_trace_id_for_seed" in body
        assert "query_exact_seed_trace" in body

    assert "seed-invalid" in source
    assert MCP_RECEIPT_FIXTURE.is_dir(), (
        "the MCP observation needs a hosted fixture whose container log is an "
        "independent tools/call receipt ledger"
    )
    server = MCP_RECEIPT_FIXTURE / "server.py"
    assert server.is_file()
    fixture_source = server.read_text()
    assert '"tools/call"' in fixture_source
    assert "print(" in fixture_source
    assert "flush=True" in fixture_source

    receipt = _shell_function(source, "mcp_receipt_call_count")
    assert "connector_container_for_alias" in receipt
    assert "docker logs" in receipt or "kubectl logs" in receipt
    assert "wc -l" in receipt or "count" in receipt
    for forbidden in ("tool input", "arguments", "receipt line"):
        assert forbidden not in receipt.lower(), (
            "public evidence may expose only an aggregate MCP call count"
        )


def test_product_collector_restore_restarts_and_verifies_every_seed_emitter() -> None:
    """Worker-only restore cannot prove dispatcher, API, or runner delivery."""

    source = LADDER_PATH.read_text()
    restore = _shell_function(source, "route_local_observability_to_product_collector")
    for service in (
        "curie-api",
        "curie-dispatcher",
        "curie-worker",
        "curie-runner",
    ):
        assert service in restore, (
            f"{service} can keep the disposable sink endpoint unless it is recreated "
            "or launched after product routing is restored"
        )
    assert "assert_product_collector_endpoint" in restore
    for protocol in ("http/protobuf", "otel-collector:4318"):
        assert protocol in restore


def test_invalid_langfuse_auth_is_temporary_durable_and_recovers_the_same_id() -> None:
    """Pin the real backend negative without redefining exporter semantics."""

    source = LADDER_PATH.read_text()
    negative = _shell_function(source, "case_local_langfuse_invalid_auth")

    # Observed shipped behavior, not an assumed external-API contract:
    # otel/collector-config.yaml enables retry_on_failure at 5s/30s/5m and a
    # file_storage-backed sending_queue of 1000. Therefore invalid auth is a
    # bounded temporary absence, and success means recovery of the same queued
    # trace ID after restoring auth, never permanent loss or a fresh control ID.
    for required in (
        "LANGFUSE_OTLP_AUTH_HEADER",
        "INVALID_LANGFUSE_OTLP_AUTH_HEADER",
        "langfuse-web",
        "otelcol_receiver_accepted_spans",
        "otelcol_exporter_send_failed_spans",
        "sending_queue",
        "file_storage",
        "queue_size",
        "Ready",
        "restart",
        "same_queued_trace_id",
        "query_exact_seed_trace",
        "queue_drained",
    ):
        assert required in negative, f"invalid-auth proof omits {required!r}"
    assert "down -v" not in negative
    assert negative.index("INVALID_LANGFUSE_OTLP_AUTH_HEADER") < negative.index(
        "same_queued_trace_id"
    )
    assert negative.count("same_queued_trace_id") >= 2, (
        "the ID observed absent while auth is invalid must be the ID recovered "
        "after the queue-preserving restart"
    )


def test_cluster_product_observability_is_private_preflight_and_query_only() -> None:
    """The ladder may inspect a task release but must never install or remove it."""

    source = LADDER_PATH.read_text()
    preflight = _shell_function(source, "preflight_cluster_product_observability")
    for required in (
        "CURIE_NAMESPACE",
        "CURIE_RELEASE",
        "default",
        "curie",
        "langfuse",
        "otel-collector",
        "api",
        "worker",
        "runner-prewarm",
        "imageID",
        "docker image inspect",
        "curie-api:local",
        "curie-worker:local",
        "curie-runner:latest",
        "mismatch",
    ):
        assert required in preflight, f"cluster preflight omits {required!r}"

    mode = _shell_function(source, "run_cluster_product_observability")
    assert "preflight_cluster_product_observability" in mode
    assert "seed_ordinary_turn" in mode
    assert "seed_approval_resume_turn" in mode
    assert "cluster observability run" in mode
    for mutating in (
        "cluster up",
        "cluster down",
        "helm install",
        "helm upgrade",
        "helm uninstall",
        "kubectl delete",
    ):
        assert mutating not in mode, (
            f"product-observability mode must be preflight/query-only, found {mutating!r}"
        )


def test_adopted_component_stop_requires_successful_export_on_both_product_surfaces() -> None:
    """Selective ingest loss is a dependency blocker only after Curie is cleared."""

    source = LADDER_PATH.read_text()
    decision = _shell_function(source, "classify_product_observability_owner")
    for required in (
        "raw_emitted_observations",
        "otelcol_receiver_accepted_spans",
        "otelcol_exporter_sent_spans",
        "langfuse_observation_membership",
        "local",
        "cluster",
        "image_ids_match",
        "seed_valid",
        "adopted-component",
    ):
        assert required in decision, f"ownership classification omits {required!r}"
    assert decision.index("otelcol_exporter_sent_spans") < decision.index(
        "adopted-component"
    )
    assert decision.index("langfuse_observation_membership") < decision.index(
        "adopted-component"
    )
