"""Contract tests for the shared Valkey test helper."""

from __future__ import annotations

import socket

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


def test_connect_or_skip_fails_for_an_unreachable_valkey_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_REQUIRE_VALKEY_TESTS", "")
    listener = _configure_unreachable_valkey(monkeypatch)

    with listener:
        with pytest.raises(redis.exceptions.ConnectionError):
            valkey.connect_or_skip()
