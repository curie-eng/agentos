"""The queued turn payload: the normalized inbound job an ingress adapter
enqueues and the worker consumes.

This is the channel-neutral promotion of the dispatcher's former
``QueuedSlackEvent`` (issue #7). It lives here, in the frozen ACI package, so the
contract is shared across all three languages (Pydantic source of truth, with
generated TypeScript and Rust derived from the committed JSON Schema) and guarded
by the schema-compat gate, rather than hand-mirrored between the Python producer
and the Rust CLI.

The field names are channel-agnostic so a second ingress adapter (not just Slack)
can produce and route the same payload:

    event_id        idempotency key for the delivery
    conversation_id the conversation/thread key routing keeps one live session per
    author          who authored the message
    text            the message text
    reply_handle    the routing pair plus where the reply is delivered (see
                    ``ReplyHandle``)
    received_at     ISO-8601 UTC timestamp of when the adapter received it

For the Slack adapter today, ``event_id`` is the Slack event id, ``conversation_id``
is the thread ts, ``author`` is the Slack user id, and ``reply_handle`` carries the
Slack channel plus the placeholder message ts. The Valkey Stream wire encoding (a
single ``payload`` field holding this model's JSON) is a transport detail and
stays outside this package, in the dispatcher's queue module.
"""

from .events import _AciModel


class ReplyHandle(_AciModel):
    """Channel-neutral coordinates for where a turn's reply is delivered.

    ``kind`` and ``channel`` are the **routing pair** (ADR-0096): the channel kind
    (``slack``, ``email``, ...) plus the address within that kind. Both halves are
    needed to resolve the binding, because one address can legitimately exist under
    two kinds. ``kind`` is REQUIRED and deliberately has no default: an optional
    kind forces the resolver to invent one, and every honest answer there is an
    address-only fallback or a ``"slack"`` guess -- the silent misroute this field
    exists to close.

    The reply model supports editing an existing reply or carrying no existing
    reply reference. ``placeholder`` is a required, nullable, opaque correlation
    handle minted by the adapter. The worker never parses it and only ever hands
    it back to the adapter that minted it. For the Slack adapter it happens to be
    the ts of the preposted placeholder message. For another kind it is whatever
    that adapter needs to find the same message again. ``None`` means this turn
    has no existing reply to edit.

    ``endpoint`` is the per-turn reply target: the base URL of the channel API the
    worker delivers this turn's reply through. It routes the reply back to the
    ingress that enqueued the turn instead of a worker-global setting, so two
    ingress paths (a real Slack workspace and a no-Slack CLI stub) can coexist on
    one worker (issue #19). ``None`` means "use the worker's configured default"
    (its ``slack_api_base_url``, i.e. real Slack), so a producer that does not set
    it keeps the pre-#19 behavior.

    ``adapter`` names the egress adapter identity whose credential authenticates
    the reply, so a sink call made *before* binding resolution can still select the
    right credential. For non-Slack kinds ``endpoint`` and ``adapter`` are both
    **platform-set from the binding row** (never accepted from an ingress request
    body); ``slack`` legitimately carries neither, because its route is the
    worker's configured Slack origin. ``adapter`` is optional at the schema so a
    third-party or pre-upgrade producer is not rejected outright, but every
    first-party mint site sets it explicitly.
    """

    kind: str
    channel: str
    placeholder: str | None
    endpoint: str | None = None
    adapter: str | None = None


class QueuedTurn(_AciModel):
    """A normalized inbound turn ready for the worker to route and run.

    The channel-neutral promotion of the dispatcher's former ``QueuedSlackEvent``.
    The Valkey Stream carries this model as a single ``payload`` JSON field; the
    stream-encoding helpers live with the producer (the dispatcher), not on this
    frozen model, so the contract stays transport-agnostic.
    """

    event_id: str
    conversation_id: str
    author: str
    text: str
    reply_handle: ReplyHandle
    received_at: str
