"""Shared fixtures. Stream/dedupe tests run against the REAL Valkey from the
compose stack (per repo test discipline: never mock Valkey). The Slack Web API
and socket transport are faked; `_black_hole_api` is not a fake but a real
loopback socket standing in for an endpoint that never answers."""

import logging
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import redis
from curie_dispatcher.config import DispatcherConfig
from curie_test_support.valkey import (
    VALKEY_HOST as _VALKEY_HOST,
)
from curie_test_support.valkey import (
    VALKEY_PORT as _VALKEY_PORT,
)
from curie_test_support.valkey import (
    VALKEY_PW as _VALKEY_PW,
)
from curie_test_support.valkey import (
    connect_or_skip,
)
from slack_bolt.authorization import AuthorizeResult


def _authorize(**_kwargs: Any) -> AuthorizeResult:
    """Shared authorization stub: Bolt's ``authorize`` callback resolved to a
    fixed bot identity, so Socket Mode tests skip the real auth.test call."""
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


class FakeSocketClient:
    """Captures the envelope acks Bolt sends back over the socket."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("fake-socket")
        self.acked_envelope_ids: list[str] = []
        # The ack BODY, not just the id (#1053). A block_actions ack is empty,
        # but a view_submission ack is a channel in its own right: it is where a
        # refused submission's reason is rendered, since the approver is standing
        # in an open modal and an ephemeral would post behind it. A test cannot
        # assert that from the envelope id alone.
        self.ack_payloads: dict[str, Any] = {}

    def send_socket_mode_response(self, response: Any) -> None:
        self.acked_envelope_ids.append(response.envelope_id)
        self.ack_payloads[response.envelope_id] = getattr(response, "payload", None)

    def ack_payload_for(self, envelope_id: str) -> Any:
        """The body this envelope was acked with, or None."""

        return self.ack_payloads.get(envelope_id)


@contextmanager
def _black_hole_api() -> Iterator[str]:
    """A real port that completes the TCP handshake and then never answers.

    Nothing accepts the connection; the kernel's listen backlog completes the
    handshake, so `connect` succeeds and the client is left reading from a
    socket no one writes to. A refused connection fails instantly and can
    never show a probe running past its deadline, so this is the fixture for
    the opposite case: a probe whose read phase is what has to give up,
    exactly the scenario an unbounded probe overshoots on. The preflight
    suite is one consumer of this.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


# Compose defaults and connection params come from the shared curie_test_support.valkey helper.


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def config(redis_client: redis.Redis) -> Iterator[DispatcherConfig]:
    """A config with a per-test-unique stream and dedupe prefix so tests do not
    collide, cleaned up afterwards."""
    token = uuid.uuid4().hex
    cfg = DispatcherConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        valkey_host=_VALKEY_HOST,
        valkey_port=_VALKEY_PORT,
        valkey_password=_VALKEY_PW,
        stream=f"test:curie:runs:{token}",
        dedupe_prefix=f"test:curie:dedupe:{token}:",
        dedupe_ttl_seconds=60,
        placeholder_text="Working on it.",
    )
    yield cfg
    keys = list(redis_client.scan_iter(f"test:curie:dedupe:{token}:*"))
    keys.append(cfg.stream)
    if keys:
        redis_client.delete(*keys)
