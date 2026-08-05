"""End-to-end dispatch through Bolt's real Socket Mode handler, offline.

We drive ``SocketModeHandler.handle`` with a fake socket client and a mocked Web
API client (the only two things faked, per test discipline), and assert the full
lifecycle step: envelope -> ack -> in-thread placeholder -> XADD to real Valkey.
"""

from typing import Any
from unittest.mock import MagicMock

import redis
from curie_dispatcher.app import build_app
from curie_dispatcher.config import DispatcherConfig
from curie_dispatcher.queue import from_stream_fields
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient

from .conftest import FakeSocketClient, _authorize

BOT_TS = "555.000"


def _build(config: DispatcherConfig, redis_client: redis.Redis) -> tuple[App, WebClient]:
    web_client = WebClient(token="xoxb-test")
    web_client.chat_postMessage = MagicMock(return_value={"ts": BOT_TS})  # type: ignore[method-assign]
    app = build_app(
        config,
        web_client=web_client,
        redis_client=redis_client,
        authorize=_authorize,
    )
    return app, web_client


def _drain(app: App) -> None:
    """Wait for Bolt's async listeners (run on a thread pool for fast ack) to finish.

    Production acks the envelope immediately and processes in the background; this
    drains that background work so assertions are deterministic.
    """
    app.listener_runner.listener_executor.shutdown(wait=True)


def _events_api_request(
    envelope_id: str,
    event_id: str,
    event: dict[str, Any],
) -> SocketModeRequest:
    return SocketModeRequest(
        type="events_api",
        envelope_id=envelope_id,
        payload={
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": event,
        },
    )


def _mention_event(text: str = "hi there") -> dict[str, Any]:
    return {
        "type": "app_mention",
        "channel": "C123",
        "user": "U123",
        "text": text,
        "ts": "1700.0001",
    }


def _block_action_request(
    envelope_id: str,
    *,
    action_id: str = "reports",
    value: str | None = None,
    trigger_id: str | None = None,
    message: dict[str, Any] | None = None,
) -> SocketModeRequest:
    action: dict[str, Any] = {"type": "button", "action_id": action_id, "action_ts": "1.5"}
    if value is not None:
        action["value"] = value
    return SocketModeRequest(
        type="interactive",
        envelope_id=envelope_id,
        payload={
            "type": "block_actions",
            "trigger_id": trigger_id or f"trig-{envelope_id}",
            "team": {"id": "T1"},
            "user": {"id": "U123"},
            "api_app_id": "A1",
            "token": "verif",
            "container": {"type": "message", "message_ts": "1700.0001"},
            "channel": {"id": "C123"},
            "message": message or {"ts": "1700.0001", "thread_ts": "1700.0001"},
            "actions": [action],
        },
    )


def test_envelope_acked_placeholder_posted_and_enqueued(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _events_api_request("env-1", "Ev-100", _mention_event()))

    # 1) The envelope was acked (fast-ack path), before background work completes.
    assert sock.acked_envelope_ids == ["env-1"]

    _drain(app)

    # 2) A placeholder was posted in-thread (root ts becomes the thread key).
    web_client.chat_postMessage.assert_called_once_with(
        channel="C123", thread_ts="1700.0001", text=config.placeholder_text
    )

    # 3) Exactly one job was enqueued, carrying the placeholder ts for the worker.
    assert redis_client.xlen(config.stream) == 1
    _, fields = redis_client.xrange(config.stream)[0]
    queued = from_stream_fields(fields)
    assert queued.event_id == "Ev-100"
    assert queued.reply_handle.channel == "C123"
    assert queued.author == "U123"
    assert queued.text == "hi there"
    assert queued.conversation_id == "1700.0001"
    assert queued.reply_handle.placeholder == BOT_TS


def test_shimmer_sets_assistant_status_after_placeholder(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    shimmer_config = config.model_copy(update={"shimmer": True})
    app, web_client = _build(shimmer_config, redis_client)
    web_client.assistant_threads_setStatus = MagicMock()  # type: ignore[method-assign]
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _events_api_request("env-1", "Ev-shim", _mention_event()))
    _drain(app)

    # The shimmer status is set on the same thread as the placeholder, and it
    # carries status_text -- NOT placeholder_text. Slack renders the status as
    # "<App Name> <status>" and inserts the app name itself, so reusing the
    # message body produced "Curie On it. Working on your request.".
    web_client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C123", thread_ts="1700.0001", status=shimmer_config.status_text
    )
    assert shimmer_config.status_text != shimmer_config.placeholder_text
    # And the normal placeholder + enqueue still happen.
    assert redis_client.xlen(config.stream) == 1


def test_shimmer_is_on_by_default_with_no_explicit_config(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """The shipped default shimmers (#1182), not just an explicitly-enabled config.

    Deliberately does NOT model_copy the flag on: the point is that an operator
    who configures nothing still gets the liveness caption, because during a turn
    the placeholder only moves when the model emits text and a reasoning model can
    stay silent for tens of seconds before its first token.
    """
    assert config.shimmer is True, "the fixture must carry the shipped default"
    app, web_client = _build(config, redis_client)
    web_client.assistant_threads_setStatus = MagicMock()  # type: ignore[method-assign]
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _events_api_request("env-1", "Ev-default-shim", _mention_event()))
    _drain(app)

    web_client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C123", thread_ts="1700.0001", status=config.status_text
    )


def test_duplicate_delivery_enqueues_exactly_once(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # Same Slack event id delivered twice (a Slack retry).
    req_first = _events_api_request("env-1", "Ev-dup", _mention_event())
    req_retry = _events_api_request("env-2", "Ev-dup", _mention_event())
    handler.handle(sock, req_first)
    handler.handle(sock, req_retry)
    _drain(app)

    # Both envelopes are acked, but only one job is enqueued and one placeholder posted.
    assert sock.acked_envelope_ids == ["env-1", "env-2"]
    assert web_client.chat_postMessage.call_count == 1
    assert redis_client.xlen(config.stream) == 1


def test_message_in_dm_is_enqueued(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    dm_event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U9",
        "text": "dm to the bot",
        "ts": "1800.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-dm", dm_event))
    _drain(app)

    assert redis_client.xlen(config.stream) == 1


def test_message_in_channel_is_ignored(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    channel_event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U9",
        "text": "just chatting, not for the bot",
        "ts": "1900.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-chan", channel_event))
    _drain(app)

    # Ordinary channel chatter is acked but never enqueued.
    assert sock.acked_envelope_ids == ["env-1"]
    assert redis_client.xlen(config.stream) == 0


def test_button_click_enqueues_a_turn(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _block_action_request("env-1", action_id="reports"))
    _drain(app)

    # A placeholder is posted in the clicked message's thread, and one turn is
    # enqueued whose text is the button's command (its action_id here).
    web_client.chat_postMessage.assert_called_once_with(
        channel="C123", thread_ts="1700.0001", text=config.placeholder_text
    )
    assert redis_client.xlen(config.stream) == 1
    _, fields = redis_client.xrange(config.stream)[0]
    queued = from_stream_fields(fields)
    assert queued.text == "reports"
    assert queued.reply_handle.channel == "C123"
    assert queued.conversation_id == "1700.0001"
    assert queued.reply_handle.placeholder == BOT_TS
    assert queued.event_id == "action-trig-env-1"


def test_button_click_prefers_value_over_action_id(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _block_action_request("env-1", action_id="btn", value="show top 5"))
    _drain(app)

    _, fields = redis_client.xrange(config.stream)[0]
    assert from_stream_fields(fields).text == "show top 5"


def test_duplicate_click_enqueues_exactly_once(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # Same interaction (trigger_id) redelivered: dedupe drops the second.
    handler.handle(sock, _block_action_request("env-1", trigger_id="trig-dup"))
    handler.handle(sock, _block_action_request("env-2", trigger_id="trig-dup"))
    _drain(app)

    assert web_client.chat_postMessage.call_count == 1
    assert redis_client.xlen(config.stream) == 1


def _home_tab_action_request(
    envelope_id: str,
    *,
    action_id: str = "reports",
    trigger_id: str | None = None,
) -> SocketModeRequest:
    """A block action from an App Home tab: container is a view, and the payload
    carries no ``channel`` and no ``message`` (the shape that KeyErrored)."""
    return SocketModeRequest(
        type="interactive",
        envelope_id=envelope_id,
        payload={
            "type": "block_actions",
            "trigger_id": trigger_id or f"trig-{envelope_id}",
            "team": {"id": "T1"},
            "user": {"id": "U123"},
            "api_app_id": "A1",
            "token": "verif",
            "container": {"type": "view", "view_id": "V1"},
            "view": {"id": "V1", "type": "home"},
            "actions": [{"type": "button", "action_id": action_id, "action_ts": "1.5"}],
        },
    )


def test_channel_less_action_is_skipped_without_burning_idempotency_key(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, web_client = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # A Home-tab click (no channel, no message) must not KeyError, must not post a
    # placeholder, and must not enqueue -- there is no thread to answer in.
    handler.handle(sock, _home_tab_action_request("env-1", trigger_id="trig-home"))
    _drain(app)

    assert sock.acked_envelope_ids == ["env-1"]  # Bolt still acked the envelope
    assert web_client.chat_postMessage.call_count == 0
    assert redis_client.xlen(config.stream) == 0

    # The idempotency key was NOT claimed: no dedupe key was written, so a Slack
    # redelivery of this interaction is not silently dropped. (A burned key here
    # would linger for the TTL and drop the redelivery.)
    dedupe_key = f"{config.dedupe_prefix}action-trig-home"
    assert redis_client.exists(dedupe_key) == 0


def test_bot_authored_message_is_ignored(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    app, _ = _build(config, redis_client)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    # The dispatcher's own placeholder shows up as a bot message; it must not loop.
    bot_event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "bot_id": "B1",
        "text": "Working on it.",
        "ts": "2000.0001",
    }
    handler.handle(sock, _events_api_request("env-1", "Ev-bot", bot_event))
    _drain(app)

    assert redis_client.xlen(config.stream) == 0
