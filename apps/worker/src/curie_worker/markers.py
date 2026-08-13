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
"""

from __future__ import annotations

import json
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


class Markers:
    """Idempotency, side-effect and completion markers over Valkey."""

    def __init__(self, redis: Redis, config: WorkerConfig) -> None:
        self._redis = redis
        self._config = config

    async def is_done(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.done_key(event_id)))

    async def mark_done(self, event_id: str, *, also_flag_completion: str | None = None) -> None:
        """Mark the event durably done, optionally flagging its outbox record.

        ``also_flag_completion`` makes the two writes ONE round trip so they
        cannot diverge: a record flagged done without the marker would let a
        sweeper emit for a turn that is about to rerun, and a marker without the
        flag would strand the record behind a guard that can never pass once
        ``done_key`` expires at ``idempotency_ttl_s``.
        """
        if also_flag_completion is None:
            await self._redis.set(
                self._config.done_key(event_id), "1", ex=self._config.idempotency_ttl_s
            )
            return
        await self._redis.eval(
            _MARK_DONE_LUA,
            2,
            self._config.done_key(event_id),
            self._config.completion_key(also_flag_completion),
            str(self._config.idempotency_ttl_s),
            _DONE_FIELD,
        )

    async def saw_side_effect(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.side_effect_key(event_id)))

    async def mark_side_effect(self, event_id: str) -> None:
        await self._redis.set(
            self._config.side_effect_key(event_id), "1", ex=self._config.idempotency_ttl_s
        )

    # -- the completion outbox ------------------------------------------------

    async def mark_completion_pending(self, event_id: str, record: CompletionRecord) -> None:
        """Write the record and index it, before the turn is marked done."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self._config.completion_key(event_id),
                mapping={
                    _RECORD_FIELD: record.model_dump_json(),
                    _DONE_FIELD: "1" if record.done else "0",
                },
            )
            pipe.sadd(self._config.completions_pending_key(), event_id)
            await pipe.execute()

    async def read_completion(self, event_id: str) -> CompletionRecord | None:
        """The stored record, or None when some emitter already cleared it."""
        stored: dict[Any, Any] = await self._redis.hgetall(
            self._config.completion_key(event_id)
        )
        raw = stored.get(_RECORD_FIELD)
        if not raw:
            return None
        record = CompletionRecord.model_validate(json.loads(raw))
        flag = stored.get(_DONE_FIELD)
        if flag is None:
            # No flag at all: a record written by a pre-upgrade worker. Its own
            # serialized value stands, and the sweeper falls back to ``is_done``.
            return record
        return record.model_copy(update={"done": flag == "1"})

    async def clear_completion(self, event_id: str) -> None:
        """Drop the payload and its set membership together, in one MULTI.

        Together, so the set and the payload can never diverge durably: a member
        without a payload is a completion nothing can reconstruct a route for.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._config.completion_key(event_id))
            pipe.srem(self._config.completions_pending_key(), event_id)
            await pipe.execute()

    async def pending_completions(self) -> set[str]:
        members: set[Any] = await self._redis.smembers(
            self._config.completions_pending_key()
        )
        return {m.decode() if isinstance(m, bytes) else str(m) for m in members}
