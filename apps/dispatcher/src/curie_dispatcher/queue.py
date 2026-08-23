"""The queue seam between the dispatcher and the worker (F1).

``QueuedTurn`` (from ``aci_protocol``) is the normalized job the dispatcher
enqueues onto the Valkey Stream and the worker consumes. The model was promoted
into the frozen ACI package (issue #7) so the contract is shared across three
languages and guarded by the schema-compat gate rather than hand-mirrored. This
module owns only the Valkey Stream *transport* of that model (a transport detail
that stays out of the frozen package): the ``payload`` wire encoding, optional
W3C trace carrier metadata, and the dedupe guard.

Wire encoding: a Stream entry carries the model as a ``payload`` field holding
``model_dump_json()`` and, while a producer span is active, a separate optional
``trace_context`` field. The model remains one JSON blob and the worker
reconstructs it from only ``payload`` with ``from_stream_fields``; legacy
payload-only entries therefore remain valid.

Dedupe (idempotency, detailed-architecture 2b rule 5): the idempotency key is the
delivery's ``event_id`` (the Slack event id for the Slack adapter). A retried
delivery must not enqueue twice. We guard with a Valkey ``SET <dedupe_key> 1 NX
EX <ttl>`` before enqueuing: the first delivery claims the key and enqueues; a
retry finds the key present and is dropped. This is chosen over stream-side
dedupe because it is O(1), TTL-bounded (no unbounded dedupe set to prune), and
does not require scanning the Stream.
"""

from typing import Any, Protocol, cast, runtime_checkable

from aci_protocol import STREAM_PAYLOAD_FIELD, QueuedTurn, parse_queued_turn
from curie_telemetry import TRACE_CONTEXT_FIELD, inject_trace_context

from .config import DispatcherConfig


@runtime_checkable
class StreamPublisher(Protocol):
    """The stream-broker port (producer side): append an entry + dedupe-claim.

    Issue #284 / ADR-0027. The producer's whole coupling to the broker is these
    two verbs: ``xadd`` (append the one-``payload`` entry to the stream) and a
    ``SET NX EX`` dedupe-claim (``set``). Typing the producer against this
    Protocol makes a future non-redis broker a drop-in on the write side. The
    consumer side has its own port (``StreamBroker`` in the worker). ``redis.Redis``
    structurally satisfies this, so it is the one backing today with no adapter.
    The verbs are typed permissively to match redis-py's signatures.
    """

    def xadd(self, name: Any, fields: Any) -> Any:
        """Append an entry to ``name``; returns the assigned stream id."""
        ...

    def set(self, name: Any, value: Any, *, nx: bool = ..., ex: Any = ...) -> Any:
        """``SET`` with ``NX``/``EX`` — the dedupe-claim beside the stream."""
        ...


def to_stream_fields(turn: QueuedTurn) -> dict[str, str]:
    """Render payload plus optional causal W3C transport metadata."""

    fields = {STREAM_PAYLOAD_FIELD: turn.model_dump_json()}
    carrier = inject_trace_context()
    if carrier is not None:
        fields[TRACE_CONTEXT_FIELD] = carrier
    return fields


def from_stream_fields(fields: dict[str, str]) -> QueuedTurn:
    """Reconstruct a turn from a Stream entry's field map (the worker's entry point)."""
    return parse_queued_turn(fields[STREAM_PAYLOAD_FIELD])


def claim_event(redis_client: "StreamPublisher", config: "DispatcherConfig", event_id: str) -> bool:
    """Claim a Slack event id for processing, returning True on the first claim.

    Uses ``SET NX EX`` so exactly one delivery of a given event id wins; retries
    see the key already set and get False, which the caller treats as a duplicate
    to drop.
    """
    claimed = redis_client.set(
        config.dedupe_key(event_id),
        "1",
        nx=True,
        ex=config.dedupe_ttl_seconds,
    )
    return bool(claimed)


def enqueue(redis_client: "StreamPublisher", config: "DispatcherConfig", turn: QueuedTurn) -> str:
    """Append the normalized turn to the Valkey Stream, returning its Stream id."""
    # redis-py types the fields param as an invariant dict of a broad key/value
    # union, so a plain dict[str, str] does not match; cast to satisfy the stub.
    fields = cast("dict[Any, Any]", to_stream_fields(turn))
    stream_id = redis_client.xadd(config.stream, fields)
    return _as_str(stream_id)


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
