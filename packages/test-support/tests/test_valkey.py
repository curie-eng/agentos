"""Contract tests for the shared Valkey test helper."""

from __future__ import annotations

import socket
import time

import pytest
import redis
from curie_test_support import valkey


def _configure_unreachable_valkey(monkeypatch: pytest.MonkeyPatch) -> socket.socket:
    """Bind a loopback port without listening, preventing a port-reuse race."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    monkeypatch.setattr(valkey, "VALKEY_HOST", "127.0.0.1")
    monkeypatch.setattr(valkey, "VALKEY_PORT", int(listener.getsockname()[1]))
    return listener


def test_connect_or_skip_skips_an_unreachable_valkey_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CI_REQUIRE_VALKEY_TESTS", raising=False)
    listener = _configure_unreachable_valkey(monkeypatch)

    with listener:
        with pytest.raises(pytest.skip.Exception, match="Valkey not reachable"):
            valkey.connect_or_skip()


# Which error an unreachable port produces is a platform detail, not a contract.
# A bound but unlistening loopback port refuses on Linux (ConnectionError) and is
# dropped on macOS (TimeoutError), so pinning ConnectionError alone made this
# test fail on every Mac. What connect_or_skip promises is that a required run
# RAISES rather than skips; both classes are that promise kept.
UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def test_connect_or_skip_fails_for_an_unreachable_valkey_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_REQUIRE_VALKEY_TESTS", "")
    listener = _configure_unreachable_valkey(monkeypatch)

    with listener:
        with pytest.raises(UNREACHABLE):
            valkey.connect_or_skip()


def test_an_unreachable_valkey_costs_the_bounded_timeout_not_a_retry_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must hold end to end, not just be passed to the constructor.

    redis-py 8.x defaults to Retry(ExponentialWithJitterBackoff(), 3), which
    multiplies socket_connect_timeout rather than capping it: measured on 8.1.0
    against a bound but unlistening port, socket_connect_timeout=5 still took
    58.74s and the unbounded default took 59.66s. So asserting the constructor
    received a timeout would not catch a reintroduced retry policy. Assert the
    wall clock instead, which is the thing that was wrong.
    """
    monkeypatch.delenv("CI_REQUIRE_VALKEY_TESTS", raising=False)
    monkeypatch.setattr(valkey, "CONNECT_TIMEOUT_SECONDS", 0.25)
    listener = _configure_unreachable_valkey(monkeypatch)

    with listener:
        started = time.monotonic()
        with pytest.raises(pytest.skip.Exception):
            valkey.connect_or_skip()
        elapsed = time.monotonic() - started

    # Generous next to a 0.25s bound, and still two orders of magnitude under the
    # ~59s the retry ladder cost.
    assert elapsed < 5, f"an unreachable Valkey took {elapsed:.2f}s, so the bound is not holding"
