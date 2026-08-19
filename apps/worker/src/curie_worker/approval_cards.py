"""Remember where an approval's Slack card was posted so it can be settled.

When the kernel pauses a run for approval (#246, ADR-0010) it posts a Block Kit
card with live Approve/Reject buttons. On resolution the dispatcher edits that
card in place from the click's interaction payload. An EXPIRY has no click
(#419): the #412 sweeper -- or a resolve attempt that arrives past the SLA --
flips the record to ``expired`` and enqueues a platform-authored resume turn,
but nothing ever touched the card, so its buttons keep looking live.

This tiny Valkey store bridges what the click payload would otherwise carry. The
kernel remembers the card destination and summary under the approval id, reads
that ref without consuming it, then removes the exact value only after the card
edit succeeds.

There is intentionally no dual read for entries keyed by thread before this
layout was introduced. Those entries remain untouched until their existing 14
day TTL expires and are not automatically settled by the new worker.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from .config import WorkerConfig

# Remove a ref only when it is still the exact value read before delivery. This
# prevents an older settlement pass from deleting a replacement ref written for
# the same approval while the card edit was in flight.
_CONSUME_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# The card outlives the turn that posted it by however long the approval SLA runs
# (hours to days), so its memory must too. A fixed, generous ceiling avoids a new
# boot-env knob: an entry that outlives this is only ever cleaned up by TTL, and a
# card whose memory lapsed simply is not auto-disabled (the resolve-click path
# still heals it on the next interaction).
DEFAULT_CARD_TTL_S = 14 * 24 * 60 * 60


@dataclass(frozen=True)
class ApprovalCardRef:
    """Where a posted approval card lives, enough to REBUILD it in place later.

    ``requested_by`` lets the settled rebuild show the same "Requested by" line
    as the live card after the sandbox is gone.

    ``kind``/``adapter`` joined ``endpoint`` for ADR-0096 phase 2: the card's
    destination is selected from the channel it POSTS TO, not from the turn that
    requested it (a policy-routed card belongs to a channel the requesting turn
    may not even share a transport with), so the settle path must re-use the
    whole destination rather than rebuild two thirds of it from the resume turn.
    ``kind`` is the discriminator for a pre-upgrade entry: ``""`` means the
    destination was never recorded, and the kernel falls back to the resume
    turn's kind and route exactly as it did before. A non-empty ``kind`` means
    the triple is authoritative, and ``adapter=None`` there means the worker's
    default transport for that kind rather than "unknown".
    """

    channel: str
    ts: str
    summary: str
    endpoint: str | None = None
    requested_by: str = ""
    kind: str = ""
    adapter: str | None = None


class ApprovalCardStore:
    """Valkey memory of posted approval cards, keyed by approval id."""

    def __init__(
        self, redis: Redis, config: WorkerConfig, *, ttl_s: int = DEFAULT_CARD_TTL_S
    ) -> None:
        self._redis = redis
        self._config = config
        self._ttl_s = ttl_s

    async def remember(
        self,
        approval_id: str,
        *,
        channel: str,
        ts: str,
        summary: str,
        endpoint: str | None,
        requested_by: str = "",
        kind: str = "",
        adapter: str | None = None,
    ) -> None:
        # Reject the empty id before it collapses every such write onto the same
        # bare approval card key.
        if not approval_id:
            raise ValueError("approval_id is required to remember an approval card")
        ref = ApprovalCardRef(
            channel=channel,
            ts=ts,
            summary=summary,
            endpoint=endpoint,
            requested_by=requested_by,
            kind=kind,
            adapter=adapter,
        )
        payload = json.dumps(asdict(ref))
        await self._redis.set(
            self._config.approval_card_key(approval_id), payload, ex=self._ttl_s
        )

    async def read(
        self, approval_id: str
    ) -> tuple[ApprovalCardRef, str | bytes] | None:
        """Return the remembered card and its exact stored value without mutation."""

        raw = await self._redis.get(self._config.approval_card_key(approval_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            ref = ApprovalCardRef(
                channel=str(data["channel"]),
                ts=str(data["ts"]),
                summary=str(data["summary"]),
                endpoint=data.get("endpoint"),
                requested_by=str(data.get("requested_by") or ""),
                kind=str(data.get("kind") or ""),
                adapter=data.get("adapter"),
            )
            return ref, raw
        except (ValueError, KeyError, TypeError):
            # A corrupt or shape-drifted entry must not break the resume; treat
            # it as no remembered card (the click path can still heal the card).
            return None

    async def consume(self, approval_id: str, expected_raw: str | bytes) -> bool:
        """Delete the ref only when its exact stored value still matches."""

        removed = await self._redis.eval(
            _CONSUME_LUA,
            1,
            self._config.approval_card_key(approval_id),
            expected_raw,
        )
        return bool(removed)
