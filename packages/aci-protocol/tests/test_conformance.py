import json
from collections.abc import Iterable

from aci_protocol import (
    PROTOCOL_VERSION,
    Event,
    Interrupt,
    reference_producer,
    run_conformance,
)


def test_reference_producer_is_conformant() -> None:
    report = run_conformance(reference_producer)
    assert report.passed, report.summary()
    assert {c.name for c in report.checks} >= {
        "outbound_roundtrip",
        "inbound_roundtrip",
        "reject_unknown_version",
        "reject_missing_version",
        "producer_stream",
    }


def test_library_checks_run_without_a_producer() -> None:
    report = run_conformance()
    assert report.passed
    assert not any(c.name == "producer_stream" for c in report.checks)


def test_a_broken_producer_fails_the_stream_check() -> None:
    def broken(_message: Event | Interrupt) -> Iterable[str]:
        return [
            json.dumps({"type": "text_delta", "version": PROTOCOL_VERSION, "text": "no final"})
            + "\n"
        ]

    report = run_conformance(broken)
    assert not report.passed
    stream_check = next(c for c in report.checks if c.name == "producer_stream")
    assert not stream_check.passed
    assert "final" in stream_check.detail
