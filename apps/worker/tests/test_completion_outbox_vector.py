"""Worker half of the frozen completion-outbox health vector (#2422)."""

from __future__ import annotations

import json
from pathlib import Path

from curie_worker.completion_health import STATUS_ARGS, snapshot_to_json
from curie_worker.config import WorkerConfig

_VECTOR = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "completion-outbox-health.json"
)
_EXPECTED_KEYS = {
    "comment",
    "default_key_prefix",
    "pending_key_suffix",
    "completion_key_infix",
    "default_grace_s",
    "dl_source",
    "status_module",
    "status_args",
    "states",
    "metric_names",
    "metric_outcomes",
}


def test_completion_outbox_health_literals_match_the_frozen_vector() -> None:
    parsed = json.loads(_VECTOR.read_text())
    unknown = set(parsed) - _EXPECTED_KEYS
    assert not unknown, (
        f"unknown keys in tests/vectors/completion-outbox-health.json: {sorted(unknown)}. "
        "Teach them to this test and to CompletionOutboxHealthVector in "
        "cli/src/completion_outbox.rs."
    )
    config = WorkerConfig()
    assert parsed["default_key_prefix"] == config.key_prefix
    assert parsed["pending_key_suffix"] == "completions:pending"
    assert config.completions_pending_key() == (
        f"{parsed['default_key_prefix']}:{parsed['pending_key_suffix']}"
    )
    assert config.completion_key("evt") == (
        f"{parsed['default_key_prefix']}:{parsed['completion_key_infix']}evt"
    )
    assert parsed["default_grace_s"] == config.completion_sweep_grace_s
    assert parsed["status_args"] == list(STATUS_ARGS)
    assert parsed["status_module"] == "curie_worker.completion_health"
    payload = snapshot_to_json(
        count=0,
        oldest_age_s=0.0,
        inflight=0,
        retry=0,
        terminal=0,
        state="empty",
        degraded=False,
    )
    assert set(payload) <= {
        "count",
        "oldest_age_s",
        "inflight",
        "retry",
        "terminal",
        "state",
        "degraded",
    }
    assert "event_id" not in payload
    assert "session" not in json.dumps(payload)
