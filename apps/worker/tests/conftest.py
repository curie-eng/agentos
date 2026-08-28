"""Shared per-test Valkey fixtures for the worker test suite.

``sync_redis`` and ``names`` are used by ``tests/kernel`` (via
``kernel/conftest.py``'s ``make_harness``) and by ``tests/test_delivery_lease.py``,
which drives the lease store directly against the same real Valkey without a
full kernel harness. Living here at the package root makes pytest resolve ONE
definition for both -- and for anything else under ``tests/`` that wants a
throwaway, collision-free Valkey namespace -- rather than two copies that can
quietly diverge, which is exactly what had already happened between them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis
from curie_test_support.valkey import connect_or_skip


@pytest.fixture
def sync_redis() -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def names(sync_redis: redis.Redis) -> Iterator[dict[str, str]]:
    """Per-test-unique stream / group / key prefixes on the shared Valkey.

    ``sandbox_prefix`` is written to by the kernel harness's sandbox substrate
    (its affinity store); ``test_delivery_lease.py`` never creates keys under
    it, so cleaning that pattern there is simply a no-op scan rather than a
    narrower override. The glob list below is the union of the two formerly
    separate copies, not a pick between them -- a dropped glob here silently
    leaks Valkey keys between tests.
    """
    token = uuid.uuid4().hex
    ns = {
        "stream": f"test:curie:runs:{token}",
        "group": f"g-{token}",
        "prefix": f"test:curie:worker:{token}",
        "sandbox_prefix": f"test:curie:sandbox:{token}",
    }
    yield ns
    for pat in (f"{ns['prefix']}*", f"{ns['sandbox_prefix']}*", ns["stream"]):
        keys = list(sync_redis.scan_iter(match=pat))
        if keys:
            sync_redis.delete(*keys)
