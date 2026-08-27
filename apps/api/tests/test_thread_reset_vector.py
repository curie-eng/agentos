"""API half of the frozen thread-reset SET vector (#1534)."""

from __future__ import annotations

import json
from pathlib import Path

from curie_api.threadreset import THREAD_RESET_INFLIGHT_SET, THREAD_RESET_SET

_VECTOR = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "thread-reset-set.json"
)
_EXPECTED_KEYS = {"comment", "thread_reset_set", "thread_reset_inflight_set"}


def test_thread_reset_keys_match_the_frozen_vector() -> None:
    parsed = json.loads(_VECTOR.read_text())
    unknown = set(parsed) - _EXPECTED_KEYS
    assert not unknown, (
        f"unknown keys in tests/vectors/thread-reset-set.json: {sorted(unknown)}. "
        "Teach them to this test, apps/worker/tests/test_thread_reset_vector.py, "
        "and the CLI queue.rs vector test."
    )
    assert parsed["thread_reset_set"] == THREAD_RESET_SET
    assert parsed["thread_reset_inflight_set"] == THREAD_RESET_INFLIGHT_SET
