"""Offline unit tests for the pure sandbox-resilience helpers.

These run with no cluster and no dev stack: they exercise only the pure
functions in ``resilience_harness.py`` (``thread_hash``, ``unique_marker``,
``final_frame``, ``collected_text``, ``detect_cross_talk``). They are deliberately
not gated by ``CURIE_SANDBOX_E2E`` so the harness logic stays covered in default
CI collection.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from resilience_harness import (  # noqa: E402
    cache_reads_for_trace,
    collected_text,
    detect_cross_talk,
    final_frame,
    thread_hash,
    unique_marker,
)


def test_thread_hash_matches_sha256_prefix() -> None:
    key = "soak-thread-42"
    expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    assert thread_hash(key) == expected
    assert len(thread_hash(key)) == 10


def test_thread_hash_distinct_keys_distinct_hashes() -> None:
    assert thread_hash("soak-thread-1") != thread_hash("soak-thread-2")


def test_unique_marker_is_deterministic_per_seed() -> None:
    assert unique_marker("phase-a", 3) == unique_marker("phase-a", 3)


def test_unique_marker_is_unique_across_seeds() -> None:
    markers = {unique_marker("phase-a", seed) for seed in range(50)}
    assert len(markers) == 50


def test_unique_marker_format() -> None:
    marker = unique_marker("phase-a", 7)
    assert marker.startswith("soakmark-phase-a-7-")
    assert " " not in marker


def test_final_frame_picks_last_final() -> None:
    frames: list[dict[str, object]] = [
        {"type": "text_delta", "text": "thinking"},
        {"type": "final", "text": "first final"},
        {"type": "text_delta", "text": "more"},
        {"type": "final", "text": "second final"},
    ]
    result = final_frame(frames)
    assert result is not None
    assert result["text"] == "second final"


def test_final_frame_none_when_absent() -> None:
    frames: list[dict[str, object]] = [{"type": "text_delta", "text": "no final here"}]
    assert final_frame(frames) is None


def test_collected_text_concatenates_text_fields() -> None:
    frames: list[dict[str, object]] = [
        {"type": "text_delta", "text": "hello"},
        {"type": "tool_note", "text": "searching", "tool": "search"},
        {"type": "final", "text": "world", "status": "done"},
    ]
    assert collected_text(frames) == "hello searching world"


def test_collected_text_ignores_non_text_frames() -> None:
    frames: list[dict[str, object]] = [
        {"type": "final", "text": "only this", "status": "done"},
        {"type": "side_effect_flag"},
        {"type": "text_delta", "text": ""},
    ]
    assert collected_text(frames) == "only this"


def test_detect_cross_talk_true_when_foreign_marker_present() -> None:
    own = "soakmark-a-0-aaaa"
    others = [own, "soakmark-b-1-bbbb"]
    text = f"reply carrying {own} and leaked soakmark-b-1-bbbb"
    assert detect_cross_talk(own, others, text) is True


def test_detect_cross_talk_false_when_only_own_marker_present() -> None:
    own = "soakmark-a-0-aaaa"
    others = [own, "soakmark-b-1-bbbb"]
    text = f"clean reply carrying only {own}"
    assert detect_cross_talk(own, others, text) is False


def test_detect_cross_talk_false_when_no_markers_present() -> None:
    own = "soakmark-a-0-aaaa"
    others = [own, "soakmark-b-1-bbbb"]
    assert detect_cross_talk(own, others, "no markers at all") is False


def test_cache_reads_are_attributed_only_to_the_requested_generation_trace() -> None:
    observations: list[dict[str, object]] = [
        {"traceId": "ours", "type": "GENERATION", "usageDetails": {"input_cached_tokens": 27}},
        {"traceId": "ours", "type": "GENERATION", "usageDetails": {"cache_read_input_tokens": 3}},
        {"traceId": "foreign", "type": "GENERATION", "usageDetails": {"input_cached_tokens": 999}},
        {"traceId": "ours", "type": "SPAN", "usageDetails": {"input_cached_tokens": 999}},
    ]
    assert cache_reads_for_trace(observations, "ours") == 30
    assert cache_reads_for_trace(observations, "unobserved") == 0


@pytest.mark.parametrize("count", [-1, True, "27"])
def test_cache_probe_rejects_malformed_usage(count: object) -> None:
    with pytest.raises(AssertionError, match="nonnegative integer"):
        cache_reads_for_trace(
            [{
                "traceId": "ours", "type": "GENERATION",
                "usageDetails": {"input_cached_tokens": count},
            }],
            "ours",
        )
