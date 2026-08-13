"""Valkey markers for idempotency, crash-safe side effects, and the completion outbox.

Three markers, all keyed by the event id (the idempotency key the ingress
assigned):

- ``done``: set once an event has been terminally handled (streamed to a final,
  or escalated). A redelivery (Slack retry that slipped the dispatcher guard, or
  a crash-recovery reclaim of an already-finished entry) sees it and is skipped.
- ``side_effect``: set the instant a ``side_effect_flag`` frame is observed, and
  therefore durable across a worker crash. The no-retry-after-side-effects rule
  needs this to survive process death: if a reclaimed event already executed a
  side effect but never reached ``done``, it must escalate, not silently re-run.
- ``completion``: the durable outbox for ``turn.completed`` (ADR-0096 EB-B6).

The completion outbox exists because ``done`` is a one-way door. Once it lands,
the already-done skip returns before anything else on every redelivery -- so a
crash or an HTTP failure between marking done and emitting the completion would
suppress the only ``turn.completed`` that will ever exist, and redelivery could
not retry it. The record is therefore written BEFORE ``done``, flagged done in
the SAME transaction as ``done``, and cleared only after a CONFIRMED emit.

Two structural choices carry that guarantee:

- **The pending index is a SET, not a SCAN over the keyspace.** The maintenance
  loop must not scan a production Valkey, and a redelivery-only sweep would never
  reach a turn whose stream entry was already acked.
- **The record has NO expiry.** A payload TTL shorter than the retention window
  leaves a set member pointing at an expired payload -- completion permanently
  lost, silently. Retention is a decision the sweeper makes out loud instead.

The outbox also carries a DEDUPE consequence, which is what ``is_terminal``
answers: a record proves its turn finished, so a turn that owns one must not be
rerun for as long as that record can exist. Completion emit is at-least-once;
turn SIDE EFFECTS are at-most-once for the whole outbox retention window, and
that is why ``mark_done`` widens the marker's own TTL to match whenever a record
is present.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from channel_protocol.reply import TurnCompleted
from pydantic import BaseModel
from redis.asyncio import Redis

from .config import WorkerConfig
from .reply_sink import TargetRoute

# Stored fields of the completion hash. The done flag is its OWN field rather
# than a value inside the record JSON so it can be set in the same MULTI as the
# done marker: a read-modify-write of the JSON could not be atomic with it, and
# the two diverging is precisely the loss this outbox exists to prevent.
_RECORD_FIELD = "record"
_DONE_FIELD = "done"
# The record's identity, minted per write. A clear is compare-and-checked
# against it so an emitter can only ever clear the record IT read: a concurrent
# retry that rewrote the record for the same event id owns a different identity,
# and clearing that one would discard a completion nobody has delivered.
_GENERATION_FIELD = "gen"

# Set the done marker and flag the completion record done in ONE round trip.
# The record is only touched when it still exists, so a sweeper that cleared it
# concurrently is never resurrected as a payload-less key with no expiry.
_MARK_DONE_LUA = """
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('HSET', KEYS[2], ARGV[2], '1')
end
return 1
"""

# Clear the record and its set membership together, but only when the record is
# still the one the caller read. A stored generation that does not match means
# some other pass rewrote it; the safe answer is to touch nothing and let that
# pass own its own clear. An ABSENT generation is a record written before this
# field existed, and it clears unconditionally exactly as it did then.
_CLEAR_COMPLETION_LUA = """
local gen = redis.call('HGET', KEYS[1], ARGV[2])
if gen == false or gen == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('SREM', KEYS[2], ARGV[3])
  return 1
end
return 0
"""


class CompletionRecord(BaseModel):
    """A self-contained, separately retryable ``turn.completed``.

    Self-contained is the whole point: it carries the ALREADY-RESOLVED route, so
    any later emitter -- the already-done skip or a sweeper -- uses the stored
    route and never re-resolves. A sweeper draining an acked entry has no binding
    lookup available to it, and re-resolving would read a binding an operator may
    since have re-pointed.
    """

    event_id: str
    event: TurnCompleted
    route: TargetRoute
    created_at: float
    done: bool = False


@dataclass(frozen=True)
class StoredCompletion:
    """A completion record AS STORED, with the two facts the JSON cannot carry.

    ``done_flag`` keeps the three states the guard needs apart, and collapsing
    them is a live defect rather than a tidiness point: ABSENT means a record
    written before the flag field existed (the only case the ``is_done``
    fallback may apply to), ``False`` means this worker wrote it and the turn is
    NOT durably done yet, and ``True`` means it is. Reading an explicit ``False``
    through the fallback lets a CONCURRENT retry's done marker authorize a
    sweeper to emit and clear a record the retry is still mid-flight on.

    ``generation`` is the record's identity, used to compare-and-clear.
    """

    record: CompletionRecord
    done_flag: bool | None
    generation: str | None


class Markers:
    """Idempotency, side-effect and completion markers over Valkey."""

    def __init__(self, redis: Redis, config: WorkerConfig) -> None:
        self._redis = redis
        self._config = config

    async def is_done(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.done_key(event_id)))

    async def is_terminal(self, event_id: str) -> bool:
        """Has this event ALREADY been handled to a terminal state?

        The dedupe question the kernel actually needs answered, and it is wider
        than ``is_done``. ``done_key`` is one marker with one TTL; a turn that
        wrote an outbox record is also provably terminal, and that record is
        retained for ``completion_max_retention_s`` (7 days) rather than
        ``idempotency_ttl_s`` (1 day). Reading only the marker let a >24h outage
        rerun a finished turn: the startup sweep emits the record and clears it,
        and the stream entry that was never acked is then reclaimed with no
        marker and no record left to refuse it. So a DONE outbox record is
        terminal in its own right, and ``mark_done`` holds the marker itself for
        the full retention window whenever a record exists (below).

        One round trip, on the sacred path: the two reads are pipelined.
        """
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.exists(self._config.done_key(event_id))
            pipe.hget(self._config.completion_key(event_id), _DONE_FIELD)
            marker, flag = await pipe.execute()
        if marker:
            return True
        return _as_str(flag) == "1"

    async def mark_done(self, event_id: str, *, also_flag_completion: str | None = None) -> None:
        """Mark the event durably done, optionally flagging its outbox record.

        ``also_flag_completion`` makes the two writes ONE round trip so they
        cannot diverge: a record flagged done without the marker would let a
        sweeper emit for a turn that is about to rerun, and a marker without the
        flag would strand the record behind a guard that can never pass once
        ``done_key`` expires at ``idempotency_ttl_s``.

        It also widens the MARKER's own TTL to the outbox retention window, for
        the reason ``is_terminal`` states: the outbox proves this turn finished
        for 7 days, so a dedupe state that lapses after 1 day can rerun a turn
        whose completion has already been delivered. A ``mark_done`` with no
        record keeps the 1-day idempotency TTL -- there is no 7-day evidence to
        stay consistent with.
        """
        if also_flag_completion is None:
            await self._redis.set(
                self._config.done_key(event_id), "1", ex=self._config.idempotency_ttl_s
            )
            return
        ttl_s = max(
            self._config.idempotency_ttl_s, int(self._config.completion_max_retention_s)
        )
        await self._redis.eval(
            _MARK_DONE_LUA,
            2,
            self._config.done_key(event_id),
            self._config.completion_key(also_flag_completion),
            str(ttl_s),
            _DONE_FIELD,
        )

    async def saw_side_effect(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.side_effect_key(event_id)))

    async def mark_side_effect(self, event_id: str) -> None:
        await self._redis.set(
            self._config.side_effect_key(event_id), "1", ex=self._config.idempotency_ttl_s
        )

    # -- the completion outbox ------------------------------------------------

    async def mark_completion_pending(self, event_id: str, record: CompletionRecord) -> str:
        """Write the record and index it, before the turn is marked done.

        Returns the record's GENERATION, which the writer keeps so its own clear
        can be compare-and-checked against the record it wrote (a concurrent
        retry that rewrote the record owns a different one).
        """
        generation = uuid.uuid4().hex
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self._config.completion_key(event_id),
                mapping={
                    _RECORD_FIELD: record.model_dump_json(),
                    _DONE_FIELD: "1" if record.done else "0",
                    _GENERATION_FIELD: generation,
                },
            )
            pipe.sadd(self._config.completions_pending_key(), event_id)
            await pipe.execute()
        return generation

    async def read_completion(self, event_id: str) -> StoredCompletion | None:
        """The stored record AS STORED, or None when some emitter cleared it.

        The three states of the done flag are preserved rather than collapsed
        (see ``StoredCompletion``): only an ABSENT flag may fall back to the
        ``is_done`` marker, because only an absent flag proves the record
        predates the field.
        """
        stored: dict[Any, Any] = await self._redis.hgetall(
            self._config.completion_key(event_id)
        )
        raw = _as_str(stored.get(_RECORD_FIELD))
        if not raw:
            return None
        record = CompletionRecord.model_validate(json.loads(raw))
        flag = _as_str(stored.get(_DONE_FIELD))
        done_flag = None if flag is None else flag == "1"
        if done_flag is not None:
            record = record.model_copy(update={"done": done_flag})
        return StoredCompletion(
            record=record,
            done_flag=done_flag,
            generation=_as_str(stored.get(_GENERATION_FIELD)),
        )

    async def clear_completion(self, event_id: str, *, generation: str | None) -> bool:
        """Drop the payload and its set membership together, in one MULTI.

        Together, so the set and the payload can never diverge durably: a member
        without a payload is a completion nothing can reconstruct a route for.

        Compare-and-checked against ``generation``: the caller clears the record
        IT read, never whatever happens to be under the key now. Without that, a
        sweeper that read a stale record could delete the fresh one a concurrent
        retry wrote in between -- an undelivered completion discarded by a pass
        that never saw it. Returns whether anything was cleared.
        """
        cleared = await self._redis.eval(
            _CLEAR_COMPLETION_LUA,
            2,
            self._config.completion_key(event_id),
            self._config.completions_pending_key(),
            generation or "",
            _GENERATION_FIELD,
            event_id,
        )
        return bool(cleared)

    async def drop_pending_member(self, event_id: str) -> None:
        """Drop ONLY the set membership, leaving whatever key state exists.

        For the member whose payload is already gone: some emitter confirmed
        delivery and cleared it. Deleting the key here would destroy a record a
        concurrent retry may have just written under the same event id, so this
        removes the stale index entry and nothing else.
        """
        await self._redis.srem(self._config.completions_pending_key(), event_id)

    async def pending_completions(self, limit: int) -> set[str]:
        """A BOUNDED batch of pending members, never the whole set.

        ``SRANDMEMBER`` with a count, not ``SMEMBERS``: one sweep pass must cost
        a bounded number of delivery attempts, because the startup sweep runs
        against exactly the backlog an outage left behind. Random sampling also
        keeps one poison record from head-of-lining every later pass -- the
        sweeper runs on the maintenance cadence, so the remainder is drained by
        the passes that follow.
        """
        # With a count, SRANDMEMBER answers with a list; the client's signature
        # also covers the countless single-member form, hence the narrowing.
        members: Any = await self._redis.srandmember(
            self._config.completions_pending_key(), limit
        )
        if not members:
            return set()
        if not isinstance(members, list):
            members = [members]
        return {str(_as_str(m)) for m in members}


def _as_str(value: Any) -> str | None:
    """Hash values as ``str``, tolerating a client without ``decode_responses``."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)
