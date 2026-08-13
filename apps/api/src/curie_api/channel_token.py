"""Scoped ingress credential for a channel binding (ADR-0096 phase 2, #1459).

The platform mints one of these per binding and hands it to the ingress adapter
that feeds that binding, so an adapter can enqueue turns for its own route
without holding the platform key. Like ``sandbox_token`` it is an HMAC-SHA256
signature over its own claims, keyed by the shared ``api_key``, and it can never
be presented as the platform key (it is never equal to ``api_key``).

**A sibling module, not a widening of ``sandbox_token`` (plan D6).**
``sandbox_token.mint`` binds an ``agent`` claim; an adapter is bound to a
BINDING, not to an agent, so the claim set would have had to grow. Spike S3's
rule is "if the claim set must grow, escalate rather than widen the primitive",
and widening would also have broken the byte-identical api/worker twin of
``sandbox_token``. Prefix ``chn``, its own claims, nothing shared but the shape.

**The claims are ``{channel_id, generation, scope, exp}``, deliberately NOT
``(kind, address)`` (plan D5).** ``crud.update_agent_binding`` mutates the
binding row IN PLACE and ``delete_agent`` frees the pair for reuse, so a token
claiming the PAIR does not go inert when the route changes hands -- it goes live
again against the new owner. The row id is a stable identity and ``generation``
is what makes a rebind observable to a credential minted before it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

_PREFIX = "chn"

# The one scope a channel token carries today, mirrored byte-identically at the
# mint site and at the ingress dependency -- the string IS the contract, exactly
# as `state`/`state.app` are for the sandbox token.
CHANNEL_ENQUEUE_SCOPE = "channel.enqueue"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _signature(api_key: str, signing_input: str) -> str:
    digest = hmac.new(api_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return _b64url(digest)


def mint(
    api_key: str, *, channel_id: str, generation: int, scope: str, exp: int
) -> str:
    """Mint a signed token naming one binding ROW at one ``generation``.

    Deterministic: the caller supplies ``exp`` (unix seconds), so the wire form
    is a pure function of its inputs.
    """

    payload = json.dumps(
        {
            "channel_id": channel_id,
            "generation": generation,
            "scope": scope,
            "exp": exp,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload_seg = _b64url(payload)
    signing_input = f"{_PREFIX}.{payload_seg}"
    return f"{signing_input}.{_signature(api_key, signing_input)}"


def verify(
    token: str,
    api_key: str,
    *,
    channel_id: str,
    generation: int,
    scope: str,
    now: int | None = None,
) -> bool:
    """True only when ``token`` is a well-formed token signed by ``api_key`` that
    names exactly this binding row, at exactly this generation, for this scope,
    and has not expired.

    Returns False (never raises) on any malformed, tampered, wrong-key,
    wrong-claim, stale-generation or expired input -- the same contract
    ``sandbox_token.verify`` documents -- so the ingress answers 401 rather than
    500. A crash here would be an unauthenticated remote DoS on the API.
    """

    try:
        prefix, payload_seg, sig_seg = token.split(".")
    except (ValueError, AttributeError, TypeError):
        return False
    if prefix != _PREFIX:
        return False
    expected_sig = _signature(api_key, f"{_PREFIX}.{payload_seg}")
    try:
        signature_ok = hmac.compare_digest(sig_seg, expected_sig)
    except TypeError:
        return False
    if not signature_ok:
        return False
    try:
        payload = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("channel_id") != channel_id or payload.get("scope") != scope:
        return False
    # Equality, not "at least": a generation that ran BACKWARDS (a rolled-back
    # row) must not revive a token either. `bool` is an `int` in Python, so it is
    # excluded explicitly here and on `exp`.
    claimed_generation = payload.get("generation")
    if (
        not isinstance(claimed_generation, int)
        or isinstance(claimed_generation, bool)
        or claimed_generation != generation
    ):
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        return False
    current = now if now is not None else int(time.time())
    return exp > current
