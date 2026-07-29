"""The optional-note dialog on an approval card (#1053).

Same discipline as ``test_approval_actions.py``: driven through Bolt's real
``SocketModeHandler``, with only the socket, the Web API client, and the
platform API faked. What is asserted here is the behavior the plain click path
cannot have:

- a click on a note-carrying card OPENS a dialog and resolves nothing;
- submitting it resolves WITH the typed note, so the reason reaches the record
  (and from there the requester, since ``build_resume_turn`` interpolates it);
- submitting it blank resolves with no note at all, rather than an empty string;
- cancelling leaves the record pending, which is stricter than the old
  click-is-the-decision behavior and must not regress;
- and the widened claim race renders its refusal INSIDE the view, because the
  loser is standing in an open modal where an ephemeral is invisible.
"""

from typing import Any
from unittest.mock import MagicMock

import redis
from curie_dispatcher.app import build_app
from curie_dispatcher.approval_actions import (
    APPROVE_NOTE_ACTION_ID,
    NOTE_MODAL_CALLBACK_ID,
    REJECT_NOTE_ACTION_ID,
    ResolveOutcome,
)
from curie_dispatcher.config import DispatcherConfig
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient

from .conftest import FakeSocketClient, _authorize

APPROVAL_ID = "9a1e8a10-0000-0000-0000-000000001053"
CARD_TS = "1700.0042"
CARD_CHANNEL = "C_MGRS"

_CARD_MESSAGE: dict[str, Any] = {
    "ts": CARD_TS,
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Approval required"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Discount for ACME"}},
        {"type": "actions", "elements": []},
    ],
}


class ScriptedResolver:
    """Stands in for the platform API, recording the note it was handed."""

    def __init__(self, outcome: ResolveOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str | None]] = []

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str,
        actor_channel: str,
        note: str | None = None,
    ) -> ResolveOutcome:
        self.calls.append(
            {
                "approval_id": approval_id,
                "decision": decision,
                "resolved_by": resolved_by,
                "actor_channel": actor_channel,
                "note": note,
            }
        )
        return self.outcome


def _build(
    config: DispatcherConfig,
    redis_client: redis.Redis,
    resolver: ScriptedResolver,
    *,
    views_open_raises: bool = False,
) -> tuple[App, WebClient]:
    web_client = WebClient(token="xoxb-test")
    web_client.chat_postMessage = MagicMock(return_value={"ts": "555.000"})  # type: ignore[method-assign]
    web_client.chat_update = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
    web_client.chat_postEphemeral = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
    web_client.conversations_history = MagicMock(  # type: ignore[method-assign]
        return_value={"messages": [_CARD_MESSAGE]}
    )
    web_client.views_open = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("trigger_id expired") if views_open_raises else None,
        return_value={"ok": True},
    )
    app = build_app(
        config,
        web_client=web_client,
        redis_client=redis_client,
        authorize=_authorize,
        resolver=resolver,
    )
    return app, web_client


def _drain(app: App) -> None:
    app.listener_runner.listener_executor.shutdown(wait=True)


def _note_click(envelope_id: str, *, action_id: str, user: str = "U_MANAGER") -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id=envelope_id,
        payload={
            "type": "block_actions",
            "trigger_id": f"trig-{envelope_id}",
            "team": {"id": "T1"},
            "user": {"id": user},
            "api_app_id": "A1",
            "token": "verif",
            "container": {"type": "message", "message_ts": CARD_TS},
            "channel": {"id": CARD_CHANNEL},
            "message": _CARD_MESSAGE,
            "actions": [
                {
                    "type": "button",
                    "action_id": action_id,
                    "action_ts": "2.0",
                    "value": APPROVAL_ID,
                }
            ],
        },
    )


def _note_submit(
    envelope_id: str,
    *,
    note: str | None,
    decision: str = "approved",
    user: str = "U_MANAGER",
) -> SocketModeRequest:
    """A view_submission for the note dialog, carrying the same
    ``private_metadata`` the open path writes."""

    import json

    state_value: dict[str, Any] = {}
    if note is not None:
        state_value = {"note": {"note-input": {"type": "plain_text_input", "value": note}}}
    return SocketModeRequest(
        type="interactive",
        envelope_id=envelope_id,
        payload={
            "type": "view_submission",
            "team": {"id": "T1"},
            "user": {"id": user},
            "api_app_id": "A1",
            "token": "verif",
            "trigger_id": f"trig-{envelope_id}",
            "view": {
                "id": "V1",
                "type": "modal",
                "callback_id": NOTE_MODAL_CALLBACK_ID,
                "private_metadata": json.dumps(
                    {
                        "approval_id": APPROVAL_ID,
                        "channel": CARD_CHANNEL,
                        "card_ts": CARD_TS,
                        "decision": decision,
                    }
                ),
                "state": {"values": state_value},
                "title": {"type": "plain_text", "text": "Approve request"},
                "blocks": [],
            },
        },
    )


def test_a_note_click_opens_a_dialog_and_resolves_nothing(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(ResolveOutcome(status_code=200, resolved_by="U_MANAGER"))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _note_click("env-n1", action_id=APPROVE_NOTE_ACTION_ID))
    assert sock.acked_envelope_ids == ["env-n1"]
    _drain(app)

    # The decision is NOT made on the click: the whole point of the dialog is
    # that the approver can still cancel.
    assert resolver.calls == []
    web_client.views_open.assert_called_once()
    view = web_client.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == NOTE_MODAL_CALLBACK_ID
    # The note field is optional: a decision must never be blocked on typing one.
    assert view["blocks"][0]["optional"] is True
    # Everything the submit handler needs rides in private_metadata, since a
    # view_submission payload carries no channel and no message of its own.
    import json

    meta = json.loads(view["private_metadata"])
    assert meta == {
        "approval_id": APPROVAL_ID,
        "channel": CARD_CHANNEL,
        "card_ts": CARD_TS,
        "decision": "approved",
    }


def test_submitting_the_dialog_resolves_with_the_typed_note(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="approved")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _note_submit("env-n2", note="  approved for Q3  "))
    _drain(app)

    assert resolver.calls == [
        {
            "approval_id": APPROVAL_ID,
            "decision": "approved",
            "resolved_by": "U_MANAGER",
            "actor_channel": CARD_CHANNEL,
            "note": "approved for Q3",
        }
    ]
    # The note is stamped onto the card too: the approver channel is where the
    # next person looks, and a reason that only reached the requester leaves
    # that channel with a bare verdict.
    web_client.chat_update.assert_called_once()
    kwargs = web_client.chat_update.call_args.kwargs
    assert kwargs["channel"] == CARD_CHANNEL and kwargs["ts"] == CARD_TS
    assert "approved for Q3" in kwargs["text"]
    assert not any(b.get("type") == "actions" for b in kwargs["blocks"])


def test_a_blank_note_resolves_with_no_note_at_all(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    # An empty string is not the same statement as leaving it blank: it would
    # persist as a resolution_note the resume turn interpolates as "Note: .".
    resolver = ScriptedResolver(ResolveOutcome(status_code=200, resolved_by="U_MANAGER"))
    app, _ = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _note_submit("env-n3", note="   "))
    _drain(app)

    assert len(resolver.calls) == 1
    assert resolver.calls[0]["note"] is None


def test_a_reject_dialog_carries_the_rejected_decision(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(ResolveOutcome(status_code=200, resolved_by="U_MANAGER"))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _note_click("env-n4", action_id=REJECT_NOTE_ACTION_ID))
    _drain(app)

    import json

    meta = json.loads(web_client.views_open.call_args.kwargs["view"]["private_metadata"])
    assert meta["decision"] == "rejected"


def test_cancelling_the_dialog_leaves_the_record_pending(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """A cancel produces no view_submission at all, so nothing resolves.

    Driven as the real sequence -- open the dialog, then deliver nothing -- which
    is exactly what Slack sends when the approver hits Cancel. This is stricter
    than the pre-#1053 behavior (a click WAS the decision) and the strictness is
    the point, so it gets its own guard.
    """

    resolver = ScriptedResolver(ResolveOutcome(status_code=200, resolved_by="U_MANAGER"))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _note_click("env-n5", action_id=APPROVE_NOTE_ACTION_ID))
    _drain(app)

    web_client.views_open.assert_called_once()
    assert resolver.calls == []
    web_client.chat_update.assert_not_called()


def test_a_claim_race_loser_is_told_inside_the_view(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """The regression this change could most easily introduce.

    Resolution now happens at submit rather than at click, so two approvers can
    hold open dialogs. The compare-and-set still makes one win; the loser is
    inside a modal, where a chat.postEphemeral posts BEHIND the open view and is
    never seen. The refusal must therefore come back as the view's own ack.
    """

    resolver = ScriptedResolver(
        ResolveOutcome(status_code=409, resolved_by="U_FIRST", detail="already resolved")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _note_submit("env-n6", note="mine"))
    _drain(app)

    payload = sock.ack_payload_for("env-n6")
    assert payload is not None, "a refused submission must ack with a response_action"
    assert payload.get("response_action") == "errors"
    assert "U_FIRST" in next(iter(payload["errors"].values()))
    # And it must NOT fall back to the invisible surface.
    web_client.chat_postEphemeral.assert_not_called()


def test_a_refused_submission_does_not_stamp_the_card(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=403, detail="self-approval is blocked")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _note_submit("env-n7", note="please"))
    _drain(app)

    web_client.chat_update.assert_not_called()
    payload = sock.ack_payload_for("env-n7")
    assert payload is not None
    assert "self-approval is blocked" in next(iter(payload["errors"].values()))


def test_a_failed_views_open_falls_forward_and_resolves(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """A trigger_id is valid for ~3s, so views.open can genuinely fail.

    The human already expressed the decision by clicking; refusing it because an
    OPTIONAL enrichment could not be collected would leave the record pending
    with no feedback, which is worse than losing the note. So it resolves, and
    says so.
    """

    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="approved")
    )
    app, web_client = _build(config, redis_client, resolver, views_open_raises=True)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _note_click("env-n8", action_id=APPROVE_NOTE_ACTION_ID))
    _drain(app)

    assert len(resolver.calls) == 1
    assert resolver.calls[0]["note"] is None
    web_client.chat_update.assert_called_once()
    # Nothing here is silent: the approver is told the note step was skipped.
    web_client.chat_postEphemeral.assert_called_once()
    assert "note" in web_client.chat_postEphemeral.call_args.kwargs["text"].lower()
