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
from redis.backoff import NoBackoff
from redis.retry import Retry

VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")

# How long one connect attempt may block. redis-py leaves socket_connect_timeout
# at None by default, so a connect to an unreachable Valkey blocks on the OS
# default rather than on anything this repo chose.
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("TEST_VALKEY_CONNECT_TIMEOUT", "2"))

# No retries, deliberately. redis-py 8.x defaults to Retry(ExponentialWithJitter,
# 3), which is a MULTIPLIER on the timeout above and is what actually made an
# unreachable Valkey cost a minute: measured on redis-py 8.1.0 against a bound
# but unlistening port, socket_connect_timeout=5 still took 58.74s and the
# unbounded default took 59.66s, while no-retry with a bounded timeout returns in
# the timeout. Retrying is wrong for this helper regardless of the cost. Its
# whole contract is one ping that decides skip-or-raise, the compose Valkey is
# either up before the suite starts or it is not, and a helper that retries turns
# "your stack is not running" into a minute of silence per fixture.
NO_RETRY = Retry(NoBackoff(), 0)


def connect_or_skip(*, decode_responses: bool = True) -> redis.Redis:
    """Connect to the compose Valkey, skipping only in optional local loops.

    Builds a ``redis.Redis`` on the shared ``TEST_VALKEY_*`` connection params,
    then pings it. An unreachable Valkey skips a local test only when
    ``CI_REQUIRE_VALKEY_TESTS`` is absent. Its presence makes the original
    ``RedisError`` fail the required CI job instead. The caller owns the returned
    client (yield it from a fixture and ``.close()`` on teardown).

    The connect is bounded by ``CONNECT_TIMEOUT_SECONDS`` and does not retry, so
    an unreachable Valkey costs that timeout once rather than a minute. See the
    constants above for why; ``TEST_VALKEY_CONNECT_TIMEOUT`` raises the bound for
    a slow environment.
    """
    client: redis.Redis = redis.Redis(
        host=VALKEY_HOST,
        port=VALKEY_PORT,
        password=VALKEY_PW or None,
        decode_responses=decode_responses,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        retry=NO_RETRY,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        if "CI_REQUIRE_VALKEY_TESTS" in os.environ:
            raise
        pytest.skip(f"Valkey not reachable at {VALKEY_HOST}:{VALKEY_PORT}: {exc}")
    return client
