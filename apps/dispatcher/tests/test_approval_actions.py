"""Click-to-resolve through the real Bolt Socket Mode handler, offline (#246).

Same discipline as test_dispatch.py: the envelope is driven through Bolt's real
``SocketModeHandler``; only the socket, the Web API client, and the platform
API (a scripted ``ApprovalResolveClient`` stand-in) are faked. Asserts the
acceptance behaviors: an authorized click resolves and stamps the card, a
non-approver gets the ephemeral rejection, a claim-race loser gets "already
resolved by X", and the ordinary-button catch-all never double-handles an
approval click.
"""

from typing import Any
from unittest.mock import MagicMock

import redis
from curie_dispatcher.app import build_app
from curie_dispatcher.approval_actions import (
    APPROVE_ACTION_ID,
    REJECT_ACTION_ID,
    ApprovalResolveClient,
    ResolveOutcome,
)
from curie_dispatcher.config import DispatcherConfig
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient

from .conftest import FakeSocketClient, _authorize

APPROVAL_ID = "9a1e8a10-0000-0000-0000-000000000246"

_CARD_MESSAGE = {
    "ts": "1700.0042",
    "thread_ts": "1700.0001",
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Approval required"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Discount for ACME"}},
        {"type": "actions", "elements": []},
    ],
}

# Real API reason strings, copied verbatim (not imported -- the dispatcher
# does not depend on apps/api). Sources: apps/api/src/curie_api/authorizer.py
# (_SELF_APPROVAL_REASON) and apps/api/src/curie_api/slack_approvers.py (the
# channel non-membership reason and the group-lookup could-not-verify reason).
# The API side pins its own half of this contract in
# apps/api/tests/test_approvers_port.py and apps/api/tests/test_approvals.py.
_CHANNEL_NON_APPROVER_REASON = (
    "you are not an approver: resolve this from the approval's channel"
)
_SELF_APPROVAL_REASON = (
    "self-approval is blocked: the requester cannot resolve their own request"
)
_COULD_NOT_VERIFY_GROUP_REASON = (
    "could not verify approver group membership: this approval's route is "
    "bound to a Slack user group and the membership lookup failed"
)


class ScriptedResolver:
    """Stands in for the platform API: returns a scripted outcome per call."""

    def __init__(self, outcome: ResolveOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str,
        actor_channel: str,
        note: str | None = None,
    ) -> ResolveOutcome:
        # `note` is recorded, not ignored: the dialog path's whole point is that
        # the approver's reason reaches the record, and a stand-in that dropped
        # it would let that regress silently (#1053).
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
    config: DispatcherConfig, redis_client: redis.Redis, resolver: ScriptedResolver
) -> tuple[App, WebClient]:
    web_client = WebClient(token="xoxb-test")
    web_client.chat_postMessage = MagicMock(return_value={"ts": "555.000"})  # type: ignore[method-assign]
    web_client.chat_update = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
    web_client.chat_postEphemeral = MagicMock(return_value={"ok": True})  # type: ignore[method-assign]
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


def _approval_click(
    envelope_id: str, *, action_id: str, user: str = "U_MANAGER"
) -> SocketModeRequest:
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
            "container": {"type": "message", "message_ts": _CARD_MESSAGE["ts"]},
            "channel": {"id": "C_MGRS"},
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


def test_authorized_click_resolves_and_stamps_the_card(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="approved")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")
    sock = FakeSocketClient()

    handler.handle(sock, _approval_click("env-a1", action_id=APPROVE_ACTION_ID))
    assert sock.acked_envelope_ids == ["env-a1"]
    _drain(app)

    # The API was asked to resolve with the clicker's identity and channel
    # (the membership evidence the server-side authorizer checks).
    assert resolver.calls == [
        {
            "approval_id": APPROVAL_ID,
            "decision": "approved",
            "resolved_by": "U_MANAGER",
            "actor_channel": "C_MGRS",
            # The immediate pair carries no note by construction; only the
            # dialog pair can collect one (#1053).
            "note": None,
        }
    ]

    # The card was stamped in place: buttons gone, verdict context appended.
    web_client.chat_update.assert_called_once()
    kwargs = web_client.chat_update.call_args.kwargs
    assert kwargs["channel"] == "C_MGRS" and kwargs["ts"] == _CARD_MESSAGE["ts"]
    assert all(b["type"] != "actions" for b in kwargs["blocks"])
    assert "Approved by <@U_MANAGER>" in kwargs["text"]

    # No turn was enqueued and no placeholder posted: the catch-all skipped it.
    web_client.chat_postMessage.assert_not_called()
    web_client.chat_postEphemeral.assert_not_called()


def test_reject_button_resolves_with_rejected_decision(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(status_code=200, resolved_by="U_MANAGER", decision="rejected")
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _approval_click("env-r1", action_id=REJECT_ACTION_ID))
    _drain(app)

    assert resolver.calls[0]["decision"] == "rejected"
    assert "Rejected by <@U_MANAGER>" in web_client.chat_update.call_args.kwargs["text"]


def test_non_approver_rejection_renders_the_api_reason(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """Strengthened from the original #246 test. The old assertion
    (``"not an approver" in kwargs["text"]``) also passes against the
    hardcoded fixed string, so it would pass unchanged even for a
    self-approval 403. Asserting the SPECIFIC channel reason from
    ``outcome.detail`` is the only way to prove the rendering is not a
    hardcoded literal (#453 AC4).
    """

    resolver = ScriptedResolver(
        ResolveOutcome(status_code=403, detail=_CHANNEL_NON_APPROVER_REASON)
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-f1", action_id=APPROVE_ACTION_ID, user="U_OUTSIDER"),
    )
    _drain(app)

    web_client.chat_postEphemeral.assert_called_once()
    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert kwargs["user"] == "U_OUTSIDER"
    # The specific reason, not just the generic "not an approver" phrase a
    # hardcoded string could also satisfy.
    assert "resolve this from the approval's channel" in kwargs["text"]
    # The card is untouched and no turn was enqueued.
    web_client.chat_update.assert_not_called()
    web_client.chat_postMessage.assert_not_called()


def test_self_approval_rejection_is_distinguishable(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """AC4: a self-approval 403 must not read like a non-membership 403."""

    resolver = ScriptedResolver(
        ResolveOutcome(status_code=403, detail=_SELF_APPROVAL_REASON)
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-f2", action_id=APPROVE_ACTION_ID, user="U_AUTHOR"),
    )
    _drain(app)

    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert "self-approval is blocked" in kwargs["text"]
    assert "not an approver" not in kwargs["text"]


def test_could_not_verify_is_not_worded_as_a_policy_denial(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """AC5: an infrastructure/config failure must not be rendered as a policy
    denial. A clicker reading this must not be told they lack permission."""

    resolver = ScriptedResolver(
        ResolveOutcome(status_code=403, detail=_COULD_NOT_VERIFY_GROUP_REASON)
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-f3", action_id=APPROVE_ACTION_ID, user="U_OUTSIDER"),
    )
    _drain(app)

    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert "could not verify" in kwargs["text"]
    assert "not an approver" not in kwargs["text"]


def test_the_three_refusal_classes_render_distinctly(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """AC4: non-membership, self-approval, and could-not-verify are
    distinguishable to the clicker. A hardcoded fixed string can never
    satisfy this, since all three would collapse to one rendered string."""

    rendered: list[str] = []
    for i, detail in enumerate(
        (
            _CHANNEL_NON_APPROVER_REASON,
            _SELF_APPROVAL_REASON,
            _COULD_NOT_VERIFY_GROUP_REASON,
        )
    ):
        resolver = ScriptedResolver(ResolveOutcome(status_code=403, detail=detail))
        app, web_client = _build(config, redis_client, resolver)
        handler = SocketModeHandler(app, app_token="xapp-test")
        handler.handle(
            FakeSocketClient(),
            _approval_click(
                f"env-distinct-{i}", action_id=APPROVE_ACTION_ID, user="U_OUTSIDER"
            ),
        )
        _drain(app)
        rendered.append(web_client.chat_postEphemeral.call_args.kwargs["text"])

    assert len(set(rendered)) == 3


def test_403_with_empty_detail_does_not_assert_policy(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    """Edge case: when the API's detail is empty (an infrastructure failure
    with no body), the fallback must stay class-neutral rather than guessing
    at "you are not an approver" (AC5)."""

    resolver = ScriptedResolver(ResolveOutcome(status_code=403, detail=""))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-f5", action_id=APPROVE_ACTION_ID, user="U_OUTSIDER"),
    )
    _drain(app)

    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert kwargs["text"]
    assert "not an approver" not in kwargs["text"]


def test_claim_race_loser_sees_already_resolved_by_winner(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(
        ResolveOutcome(
            status_code=409,
            detail="already resolved by U_FIRST (approved)",
            resolved_by=None,
        )
    )
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(
        FakeSocketClient(),
        _approval_click("env-l1", action_id=APPROVE_ACTION_ID, user="U_SECOND"),
    )
    _drain(app)

    kwargs = web_client.chat_postEphemeral.call_args.kwargs
    assert kwargs["user"] == "U_SECOND"
    assert "already resolved by U_FIRST" in kwargs["text"]
    # The stale card is refreshed so it stops offering buttons.
    assert web_client.chat_update.call_count == 1
    assert all(
        b["type"] != "actions"
        for b in web_client.chat_update.call_args.kwargs["blocks"]
    )


def test_expired_click_gets_ephemeral_expiry_notice(
    redis_client: redis.Redis, config: DispatcherConfig
) -> None:
    resolver = ScriptedResolver(ResolveOutcome(status_code=410, detail="expired"))
    app, web_client = _build(config, redis_client, resolver)
    handler = SocketModeHandler(app, app_token="xapp-test")

    handler.handle(FakeSocketClient(), _approval_click("env-x1", action_id=APPROVE_ACTION_ID))
    _drain(app)

    assert "expired" in web_client.chat_postEphemeral.call_args.kwargs["text"]


class _FakeHttpResponse:
    """A non-JSON HTTP response body, as an intermediary (ingress/WAF) in
    front of the API might return instead of FastAPI's own JSON 403."""

    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        raise ValueError("not json")


class _FakeHttpClient:
    """Stands in for httpx.Client: the only external boundary resolve() calls."""

    def __init__(self, response: _FakeHttpResponse) -> None:
        self._response = response

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> _FakeHttpResponse:
        return self._response


def test_non_json_403_body_is_not_captured_as_detail() -> None:
    """LOW-1 (security review of #453): a non-JSON 403 body must not be
    captured into ResolveOutcome.detail. FastAPI's own 403s are always JSON,
    but an intermediary (ingress/WAF) in front of the API can return a
    non-JSON body -- an HTML block page that may embed an internal hostname
    or request id. Before this PR the 403 branch showed a hardcoded string,
    so this raw text never reached the clicker; now
    process_approval_action renders outcome.detail verbatim (#453 AC4/AC5),
    so a non-JSON body reaching resolve() must not become a renderable
    reason in the first place.
    """

    raw_body = "<html>403 Forbidden - waf-node-7.internal</html>"
    fake_client = _FakeHttpClient(_FakeHttpResponse(status_code=403, text=raw_body))
    resolver = ApprovalResolveClient(
        api_base_url="https://api.internal", api_key="k", client=fake_client
    )

    outcome = resolver.resolve(
        APPROVAL_ID, decision="approved", resolved_by="U_MANAGER", actor_channel="C_MGRS"
    )

    assert outcome.status_code == 403
    assert raw_body not in outcome.detail
