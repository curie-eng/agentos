#!/usr/bin/env python3
"""Emit minimal causal OTLP file records for the shell-stub ladder tests.

The executing Rust controls run the real ladder but replace its services with
argv-strict shell stubs.  This fixture supplies the collector file boundary
those controls cannot otherwise produce; the production ladder still has to
drive and parse every topology/outcome/control with no bypass knob.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _identifier(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _span(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str = "",
    attributes: list[dict[str, Any]] | None = None,
    events: list[dict[str, str]] | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "traceId": trace_id,
        "spanId": span_id,
        "attributes": attributes or [],
        "events": events or [],
    }
    if parent_span_id:
        record["parentSpanId"] = parent_span_id
    if kind:
        record["kind"] = kind
    if status:
        record["status"] = {"code": status}
    return record


def emit_turn(
    output: Path,
    *,
    agent_id: str,
    thread: str,
    topology: str,
    outcome: str,
    warning: str | None = None,
) -> None:
    session_id = f"agent-{agent_id}-thread-{thread}"
    trace_id = _identifier(session_id, 32)
    ids = {
        label: _identifier(f"{session_id}:{label}", 16)
        for label in ("producer", "process", "worker", "sandbox", "client", "agent")
    }
    messaging = [
        _attribute("messaging.system", "valkey"),
        _attribute("messaging.destination.name", "curie:runs"),
        _attribute("messaging.operation.type", "process"),
    ]
    spans: list[dict[str, Any]] = []
    process_parent = ""
    if topology == "dispatcher":
        spans.append(
            _span(
                name="send curie:runs",
                trace_id=trace_id,
                span_id=ids["producer"],
                attributes=[
                    _attribute("messaging.system", "valkey"),
                    _attribute("messaging.destination.name", "curie:runs"),
                    _attribute("messaging.operation.type", "send"),
                ],
            )
        )
        process_parent = ids["producer"]
    spans.extend(
        [
            _span(
                name="process curie:runs",
                trace_id=trace_id,
                span_id=ids["process"],
                parent_span_id=process_parent,
                attributes=messaging,
            ),
            _span(
                name="worker.turn",
                trace_id=trace_id,
                span_id=ids["worker"],
                parent_span_id=ids["process"],
                events=[
                    {"name": "worker.reply.final"},
                    {"name": "worker.completion.settled"},
                ],
            ),
            _span(
                name="sandbox.claim",
                trace_id=trace_id,
                span_id=ids["sandbox"],
                parent_span_id=ids["worker"],
            ),
            _span(
                name="POST /v1/event",
                trace_id=trace_id,
                span_id=ids["client"],
                parent_span_id=ids["worker"],
                kind="SPAN_KIND_CLIENT",
                attributes=[_attribute("http.request.method", "POST")],
            ),
            _span(
                name="agent.run",
                trace_id=trace_id,
                span_id=ids["agent"],
                parent_span_id=ids["client"],
                attributes=[_attribute("langfuse.session.id", session_id)],
                status=(
                    "STATUS_CODE_OK"
                    if outcome == "success"
                    else "STATUS_CODE_ERROR"
                ),
            ),
        ]
    )
    log_body = warning or (
        "turn end" if outcome == "success" else "turn failed"
    )
    log_records = [
        {
            "traceId": trace_id,
            "spanId": ids["agent"],
            "body": {"stringValue": log_body},
            "attributes": [],
        }
    ]
    output.mkdir(parents=True, exist_ok=True)
    with (output / "traces.json").open("a") as traces:
        traces.write(json.dumps({"spans": spans}, separators=(",", ":")) + "\n")
    with (output / "logs.json").open("a") as logs:
        logs.write(
            json.dumps({"logRecords": log_records}, separators=(",", ":")) + "\n"
        )


def emit_controls(output: Path, agent_id: str) -> dict[str, Any]:
    threads = {
        "dispatcher": "otel-dispatcher-example",
        "missing": "otel-missing-example",
        "malformed": "otel-malformed-example",
        "failure": "otel-failure-example",
        "recovery": "otel-recovery-example",
        "redaction": "otel-redaction-example",
    }
    for label, thread in threads.items():
        emit_turn(
            output,
            agent_id=agent_id,
            thread=thread,
            topology="dispatcher" if label == "dispatcher" else "cli",
            outcome="failure" if label == "failure" else "success",
            warning=(
                "ignored malformed trace context" if label == "malformed" else None
            ),
        )
    return {
        "threads": threads,
        "malformed_sentinel": "otel-malformed-carrier-private-value",
        "redaction_sentinel": "sk-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    turn = subparsers.add_parser("turn")
    turn.add_argument("--agent-id", required=True)
    turn.add_argument("--thread", required=True)
    turn.add_argument("--topology", choices=("cli", "dispatcher"), required=True)
    turn.add_argument("--outcome", choices=("success", "failure"), required=True)
    controls = subparsers.add_parser("controls")
    controls.add_argument("--agent-id", required=True)
    args = parser.parse_args()

    output = Path(os.environ["CURIE_OTEL_E2E_OUTPUT"])
    if args.command == "turn":
        emit_turn(
            output,
            agent_id=args.agent_id,
            thread=args.thread,
            topology=args.topology,
            outcome=args.outcome,
        )
    else:
        print(json.dumps(emit_controls(output, args.agent_id), sort_keys=True))


if __name__ == "__main__":
    main()
