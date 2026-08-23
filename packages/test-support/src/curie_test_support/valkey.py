"""Shared Valkey connect-and-skip helpers for the test suites.

The three constants read the same env vars with the same compose defaults every
test site used before consolidation (compose.dev.yaml maps Valkey to host port
26379, password ``valkeypass``); ``connect_or_skip`` is the sync build+ping
block those sites duplicated. Local developer loops can skip an unreachable
Valkey, but required CI must surface the original connection error.
"""

from __future__ import annotations

import os

import pytest
import redis

VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


def connect_or_skip(*, decode_responses: bool = True) -> redis.Redis:
    """Connect to the compose Valkey, skipping only in optional local loops.

    Builds a ``redis.Redis`` on the shared ``TEST_VALKEY_*`` connection params,
    then pings it. An unreachable Valkey skips a local test only when
    ``CI_REQUIRE_VALKEY_TESTS`` is absent. Its presence makes the original
    ``RedisError`` fail the required CI job instead. The caller owns the returned
    client (yield it from a fixture and ``.close()`` on teardown).
    """
    client: redis.Redis = redis.Redis(
        host=VALKEY_HOST,
        port=VALKEY_PORT,
        password=VALKEY_PW or None,
        decode_responses=decode_responses,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        if "CI_REQUIRE_VALKEY_TESTS" in os.environ:
            raise
        pytest.skip(f"Valkey not reachable at {VALKEY_HOST}:{VALKEY_PORT}: {exc}")
    return client
