"""Authenticated egress is a typed, bounded channel-protocol boundary."""

from __future__ import annotations

import json

import pytest
from _support import INBOX, MailState, completed, post_bytes, post_event, update
from curie_mail_adapter.adapter import MailAdapter


def _seed(mail: MailState, adapter: MailAdapter) -> None:
    mail.add_inbound("msg-1", "thr-1")
    adapter.poll_once()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"version": "0.9", "event": "turn.status", "target": {}},
        {
            "version": "1.0",
            "event": "unknown.event",
            "target": {
                "kind": "email",
                "address": INBOX,
                "conversation_id": "thr-1",
                "reply_ref": "msg-1",
            },
        },
        {
            **update("forged"),
            "target": {**update("forged")["target"], "kind": "discord"},
        },
        {
            **update("forged"),
            "target": {**update("forged")["target"], "address": "other@example.com"},
        },
    ],
)
def test_invalid_authenticated_events_are_4xx_and_have_no_side_effect(
    mail: MailState,
    adapter: MailAdapter,
    egress_url: str,
    payload: object,
) -> None:
    _seed(mail, adapter)

    status, _ = post_bytes(egress_url, json.dumps(payload).encode())

    assert 400 <= status < 500
    assert mail.replies == []
    # Rejection occurred before reply state mutation: a later valid completion
    # must not contain the forged update text.
    assert post_event(egress_url, completed("ev-valid"))[0] == 200
    assert "forged" not in mail.replies[0][1]


def test_malformed_authenticated_json_is_4xx_and_has_no_side_effect(
    mail: MailState, adapter: MailAdapter, egress_url: str
) -> None:
    _seed(mail, adapter)

    status, _ = post_bytes(egress_url, b'{"version":')

    assert 400 <= status < 500
    assert mail.replies == []


def test_oversize_authenticated_body_is_rejected_before_json_allocation(
    mail: MailState, adapter: MailAdapter, egress_url: str
) -> None:
    _seed(mail, adapter)
    oversized = json.dumps({**update("x"), "text": "x" * (2 * 1024 * 1024)}).encode()

    status, _ = post_bytes(egress_url, oversized)

    assert status in {400, 413}
    assert mail.replies == []


def test_chunked_authenticated_body_is_rejected(
    mail: MailState, adapter: MailAdapter, egress_url: str
) -> None:
    _seed(mail, adapter)

    status, _ = post_bytes(
        egress_url,
        json.dumps(update("must not land")).encode(),
        headers={"Transfer-Encoding": "chunked"},
    )

    assert 400 <= status < 500
    assert mail.replies == []
