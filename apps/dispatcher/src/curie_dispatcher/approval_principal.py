"""Local codec for Slack-authenticated approval principals (ADR-0106).

The platform API and dispatcher intentionally do not share application code,
so this module mirrors the compact credential wire format at their service
boundary. A chat principal is short lived and bound to the exact approval,
Slack user, and channel delivered over the authenticated Socket Mode session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

_PREFIX = "apr"
_SCOPE = "approval.resolve"
_CHAT_PRINCIPAL_TTL_SECONDS = 60


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def mint_chat_principal(
    secret: str,
    *,
    subject: str,
    actor_channel: str,
    approval_id: str,
    now: int | None = None,
) -> str:
    """Mint a 60-second chat attestation for one approval resolution."""

    if not secret.strip():
        raise ValueError("chat attester secret must be non-blank")
    if not subject.strip():
        raise ValueError("chat approval principal subject must be non-blank")
    if not actor_channel.strip():
        raise ValueError("chat approval principal channel must be non-blank")
    if not approval_id.strip():
        raise ValueError("chat approval principal approval id must be non-blank")

    issued_at = int(time.time()) if now is None else now
    payload = json.dumps(
        {
            "actor_channel": actor_channel,
            "approval_id": approval_id,
            "exp": issued_at + _CHAT_PRINCIPAL_TTL_SECONDS,
            "kind": "chat",
            "scope": _SCOPE,
            "sub": subject,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload_segment = _b64url(payload)
    signing_input = f"{_PREFIX}.{payload_segment}"
    signature = _b64url(
        hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    )
    return f"{signing_input}.{signature}"
