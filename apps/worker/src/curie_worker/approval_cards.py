"""Remember where an approval's Slack card was posted, so an expiry can disable it.

When the kernel pauses a run for approval (#246, ADR-0010) it posts a Block Kit
card with live Approve/Reject buttons. On resolution the dispatcher edits that
card in place from the click's interaction payload. An EXPIRY has no click
(#419): the #412 sweeper -- or a resolve attempt that arrives past the SLA --
flips the record to ``expired`` and enqueues a platform-authored resume turn,
but nothing ever touched the card, so its buttons keep looking live.

This tiny Valkey store bridges what the click payload would otherwise carry: the
kernel remembers the card's channel/ts/summary keyed by the suspended thread at
pause time, and pops it when the expiry resume turn arrives to disable the card.
Keyed by thread because a suspended thread has exactly one pending approval at a
time; a later approval on the same thread overwrites the entry, and the resume
turn (resolve OR expiry) pops it either way.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from .config import WorkerConfig

# The card outlives the turn that posted it by however long the approval SLA runs
# (hours to days), so its memory must too. A fixed, generous ceiling avoids a new
# boot-env knob: an entry that outlives this is only ever cleaned up by TTL, and a
# card whose memory lapsed simply is not auto-disabled (the resolve-click path
# still heals it on the next interaction).
DEFAULT_CARD_TTL_S = 14 * 24 * 60 * 60


@dataclass(frozen=True)
class ApprovalCardRef:
    """Where a posted approval card lives, enough to REBUILD it in place later.

    ``requested_by`` joined the ref for #1084: the settled rebuild shows the
    same "Requested by" line the live card did, and the worker has no other copy
    of it once the sandbox is gone. Defaulted so an entry remembered before
    #1084 still loads -- these live in Valkey across a deploy, and the settled
    card simply omits the line rather than failing to render.

    ``approval_id`` joined it for #1199: the entry is keyed by THREAD, so
    without it nothing tied a popped ref to the approval whose verdict is about
    to be stamped on it, and a ref overwritten (or left stale) by another
    approval on the same thread would take the wrong verdict. Defaulted for the
    same across-a-deploy reason as ``requested_by``: an entry written before
    #1199 carries no such key, and an empty value means "cannot be paired",
    which the kernel treats as stampable exactly as it was before.

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
    approval_id: str = ""
    kind: str = ""
    adapter: str | None = None


class ApprovalCardStore:
    """Valkey memory of posted approval cards, keyed by suspended thread."""

    def __init__(
        self, redis: Redis, config: WorkerConfig, *, ttl_s: int = DEFAULT_CARD_TTL_S
    ) -> None:
        self._redis = redis
        self._config = config
        self._ttl_s = ttl_s

    async def remember(
        self,
        thread: str,
        *,
        channel: str,
        ts: str,
        summary: str,
        endpoint: str | None,
        approval_id: str,
        requested_by: str = "",
        kind: str = "",
        adapter: str | None = None,
    ) -> None:
        # An empty ``approval_id`` is the pre-#1199 compatibility sentinel: the
        # kernel reads it as "cannot be paired" and stamps the ref unchecked.
        # That exemption is only defensible while "" provably means "written
        # before the field existed", so today's code must be incapable of
        # producing one. Nothing legitimate calls this without an id, and an
        # entry persisted with one is unpairable for the full TTL, so fail loudly
        # here rather than silently days later on someone else's card.
        if not approval_id:
            raise ValueError(
                "approval_id is required to remember an approval card; "
                "an empty approval_id is the pre-#1199 compatibility sentinel "
                "and must never be written by today's code"
            )
        await self._write(
            thread,
            ApprovalCardRef(
                channel=channel,
                ts=ts,
                summary=summary,
                endpoint=endpoint,
                requested_by=requested_by,
                approval_id=approval_id,
                kind=kind,
                adapter=adapter,
            ),
        )

    async def restore(self, thread: str, ref: ApprovalCardRef) -> None:
        """Put a popped ref back, but only if nothing newer took its place.

        The kernel has to pop before it can tell whether the ref belongs to the
        approval it is settling (#1199), so a ref that turns out to belong to a
        DIFFERENT, still-pending approval has to go back. Dropping it would
        strand that approval's card with live Approve/Reject buttons nothing
        will ever settle -- on the expiry path there is no click to heal it,
        which is the exact state #419 exists to remove.

        ``SET NX`` because the write is conditional, NOT because it races the
        overwrite window it is trying to survive: the ``pop`` that produced this
        ref already closed that window by emptying the key. The only way it loses is
        that a newer ``remember`` landed on the thread in between, and then the
        newer entry is the one that should survive -- declining to write is the
        correct outcome there, not a lost update. Same TTL a fresh ``remember``
        would use, so a restored ref expires on the same schedule.

        Only the refusal path restores. A successful stamp leaves the key empty,
        which is what makes the stamp exactly-once (``pop`` is GETDEL, so a
        redelivery finds nothing).
        """

        await self._write(thread, ref, nx=True)

    async def _write(
        self, thread: str, ref: ApprovalCardRef, *, nx: bool = False
    ) -> None:
        """The one writer of the entry, for both ``remember`` and ``restore``.

        The dataclass field names ARE the payload keys ``pop`` reads back, so
        ``ApprovalCardRef`` is the entry's schema: a field added to it reaches
        every writer at once, instead of a second hand-built literal silently
        dropping it.
        """

        payload = json.dumps(asdict(ref))
        await self._redis.set(
            self._config.approval_card_key(thread), payload, ex=self._ttl_s, nx=nx
        )

    async def pop(self, thread: str) -> ApprovalCardRef | None:
        """Return and delete the remembered card for this thread, or None.

        GETDEL so the memory is consumed exactly once: a redelivered resume turn
        finds nothing and no-ops, and a resolved card (the dispatcher's job) is
        cleaned up here rather than lingering to the TTL.
        """

        raw = await self._redis.getdel(self._config.approval_card_key(thread))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if "approval_id" in data and not data["approval_id"]:
                # An ABSENT key is a card remembered before the field existed;
                # a key that is PRESENT but falsy (``null``, ``0``, ``false``,
                # ``[]``) is a payload no writer of ours has ever produced --
                # shape drift, a half-populated entry, or a hostile write. A
                # coalescing read collapses both to "", and "" is precisely what
                # exempts a ref from the kernel's pairing check, so on an
                # authorization surface the two must not share an outcome. This
                # one is corrupt, and corrupt means no remembered card -- the
                # same answer the handler below gives, reached deliberately
                # rather than by a raised exception.
                return None
            return ApprovalCardRef(
                channel=str(data["channel"]),
                ts=str(data["ts"]),
                summary=str(data["summary"]),
                endpoint=data.get("endpoint"),
                # ``.get`` not ``[...]``: entries written before #1084 have no
                # such key, and a KeyError here would be caught by the corrupt
                # handler below and silently drop a perfectly usable card ref.
                requested_by=str(data.get("requested_by") or ""),
                # ``.get`` for the same reason (#1199): an entry written before
                # the pairing existed has no such key, and an empty id means the
                # kernel cannot pair it, not that it must be refused. Unlike
                # ``requested_by`` above there is no ``or ""`` here, and the
                # difference is deliberate: that field is cosmetic, so coalescing
                # a falsy value costs a line on the card, while this one gates
                # the pairing check, so only an absent key may reach "" -- a
                # present-but-falsy id is rejected outright above.
                approval_id=str(data.get("approval_id", "")),
                # ``.get`` for the across-a-deploy reason again: an entry written
                # before the destination was recorded has neither key, and ``""``
                # is what tells the kernel to fall back to the resume turn's kind
                # and route instead of treating ``adapter=None`` as authoritative.
                kind=str(data.get("kind") or ""),
                adapter=data.get("adapter"),
            )
        except (ValueError, KeyError, TypeError):
            # A corrupt or shape-drifted entry must not break the resume; treat
            # it as no remembered card (the click path can still heal the card).
            return None
