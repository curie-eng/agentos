#!/usr/bin/env python3
"""Assert causal trace and correlated-log facts from the collector file sink.

The ladder drives the turns.  This parser only judges records produced after its
watermark, so an incumbent stack's older telemetry cannot manufacture a pass.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _records(path: Path, collection: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    found: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        document = json.loads(line)
        stack: list[Any] = [document]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                items = value.get(collection)
                if isinstance(items, list):
                    found.extend(item for item in items if isinstance(item, dict))
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return found


def _attrs(record: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in record.get("attributes", []):
        value = item.get("value", {})
        result[item["key"]] = next(iter(value.values()), None)
    return result


def _id(record: dict[str, Any], key: str) -> str:
    return str(record.get(key, "")).lower()


def _body(record: dict[str, Any]) -> str:
    body = record.get("body", {})
    return str(next(iter(body.values()), ""))


def _status(record: dict[str, Any]) -> str:
    return str(record.get("status", {}).get("code", "")).upper()


def _severity(record: dict[str, Any]) -> str:
    return str(record.get("severityText", "")).upper()


def _descends(child: dict[str, Any], ancestor: dict[str, Any], spans: list[dict[str, Any]]) -> bool:
    by_id = {_id(span, "spanId"): span for span in spans}
    cursor = child
    seen: set[str] = set()
    while parent_id := _id(cursor, "parentSpanId"):
        if parent_id == _id(ancestor, "spanId"):
            return True
        if parent_id in seen or parent_id not in by_id:
            return False
        seen.add(parent_id)
        cursor = by_id[parent_id]
    return False


def assert_turn(
    spans: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    *,
    topology: str,
    outcome: str,
    session_id: str,
    forbidden: str | None,
    require_reply_completion: bool,
) -> None:
    agent = next(
        span
        for span in spans
        if span.get("name") == "agent.run"
        and _attrs(span).get("langfuse.session.id") == session_id
    )
    agent_trace_id = _id(agent, "traceId")
    process = next(
        span
        for span in spans
        if span.get("name") == "process curie:runs"
        and _id(span, "traceId") == agent_trace_id
    )
    trace_id = _id(process, "traceId")
    tree = [span for span in spans if _id(span, "traceId") == trace_id]
    worker = next(span for span in tree if span.get("name") == "worker.turn")
    client = next(
        span
        for span in tree
        if str(span.get("kind", "")).upper() in {"3", "SPAN_KIND_CLIENT"}
        and _attrs(span).get("http.request.method") == "POST"
        and _descends(agent, span, tree)
    )
    assert {
        "messaging.system": "valkey",
        "messaging.destination.name": "curie:runs",
        "messaging.operation.type": "process",
    }.items() <= _attrs(process).items()
    assert _descends(worker, process, tree)
    assert _descends(client, worker, tree)
    assert _descends(agent, client, tree)
    assert any(span.get("name") == "sandbox.claim" for span in tree)
    event_names = {
        event.get("name") for span in tree for event in span.get("events", [])
    }
    if require_reply_completion:
        assert {"worker.reply.final", "worker.completion.settled"} <= event_names

    producer = [span for span in tree if span.get("name") == "send curie:runs"]
    if topology == "cli":
        assert not producer, "curie local message bypasses the dispatcher honestly"
        assert not _id(process, "parentSpanId"), "payload-only CLI enqueue starts a root"
    else:
        assert len(producer) == 1
        assert _descends(process, producer[0], tree)
        assert {
            "messaging.system": "valkey",
            "messaging.destination.name": "curie:runs",
            "messaging.operation.type": "send",
        }.items() <= _attrs(producer[0]).items()

    expected_status = "STATUS_CODE_OK" if outcome == "success" else "STATUS_CODE_ERROR"
    assert _status(agent) in {expected_status, "1" if outcome == "success" else "2"}
    correlated = [
        record
        for record in logs
        if _id(record, "traceId") == trace_id and _id(record, "spanId")
    ]
    assert correlated, "the causal trace has no trace-correlated OTLP logs"
    if outcome == "failure":
        agent_span_id = _id(agent, "spanId")
        assert any(
            _id(record, "traceId") == agent_trace_id
            and _id(record, "spanId") == agent_span_id
            and _severity(record) == "ERROR"
            for record in logs
        ), "the failing agent.run span has no correlated ERROR-severity OTLP log"
    raw = json.dumps({"spans": tree, "logs": correlated})
    if forbidden:
        assert forbidden not in raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--topology", choices=("cli", "dispatcher"), required=True)
    parser.add_argument("--outcome", choices=("success", "failure"), required=True)
    parser.add_argument("--session-id", required=False, default="")
    parser.add_argument("--forbidden")
    parser.add_argument("--require-warning")
    parser.add_argument("--require-reply-completion", action="store_true")
    parser.add_argument("--expect-empty", action="store_true")
    args = parser.parse_args()

    spans = _records(args.traces, "spans")
    logs = _records(args.logs, "logRecords")
    if args.expect_empty:
        assert not spans and not logs, "no-endpoint control unexpectedly exported telemetry"
        print("otel-e2e no-endpoint: no records")
        return
    if args.require_warning:
        assert any(args.require_warning in _body(record) for record in logs)
        assert args.forbidden is None or all(args.forbidden not in _body(record) for record in logs)
    assert_turn(
        spans,
        logs,
        topology=args.topology,
        outcome=args.outcome,
        session_id=args.session_id,
        forbidden=args.forbidden,
        require_reply_completion=args.require_reply_completion,
    )
    print(
        f"otel-e2e topology={args.topology} outcome={args.outcome}: "
        "causal tree and logs verified"
    )


if __name__ == "__main__":
    main()
