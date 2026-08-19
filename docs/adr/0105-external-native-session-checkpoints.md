# 105. External native session checkpoints are a harness capability

Date: 2026-08-11

Status: Draft

If Accepted, this ADR supersedes two earlier decisions in part.

1. It supersedes only the preference in
   [ADR 0029](0029-conversation-history-port-and-first-loader.md) for a portable
   preamble over a clean compatible native checkpoint. Its projection remains.
2. It supersedes only decision 4 in
   [ADR 0060](0060-the-harness-is-a-declared-package.md). History gets a
   separate capability without a frozen ACI change.

[ADR 0003](0003-stateless-first-rehydrate-on-resume.md) remains authoritative:
resume is a cold rehydrate from external state, never a surviving process.

## Context

ADR 0029 persists readable user and assistant text as a portable preamble. It
survives a deleted pod and works for any harness, but omits tool calls, tool
results, provider metadata, and other native context.

The installed Anthropic Python SDK now exposes `SessionStore` and external
session materialization for prompts, tool calls, tool results, and responses.
The contracts are visible in the official
[SDK 0.2.115 types](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.115/src/claude_agent_sdk/types.py)
and [client](https://github.com/anthropics/claude-agent-sdk-python/blob/v0.2.115/src/claude_agent_sdk/client.py).

The capability is not unique to Claude. LangGraph persists thread scoped
checkpoints and pending writes through its
[checkpointer contract](https://docs.langchain.com/oss/python/langgraph/persistence).
OpenCode and other harnesses may expose different formats. Curie needs one
lifecycle and storage capability, not one provider type hierarchy.

The general state store caps one value at 64 KiB and one namespace at 1 MiB. A
native entry may be large and a session may grow past both limits. Enlarging
those caps preserves a single growing value and read modify write amplification.

[ADR 0061](0061-out-of-process-harness-boundary.md) proposes a process boundary,
so a Python `Protocol` is not the durable neutral seam. The durable seam is a
scoped HTTP resource with ordered opaque JSON log semantics.

The frozen ACI and plugin format do not need to carry provider session data or
a native session identifier. The existing deterministic history resource
already scopes storage to an agent and thread.

## Decision

### 1. Capability and provider boundary

Curie owns `HarnessContribution.native_sessions`, an optional
`NativeSessionCapability` containing `format_id`, `project_key`, and
`new_session_id`.

The capability declares compatibility identity and native session identity
construction. It does not contain provider session values. Harnesses without
the capability continue to use the portable projection only. Fake mode never
initializes the external native store.

The neutral HTTP resource provides current generation lookup, generation
creation, dirty and clean transitions, append, load, and subkey listing.

Curie stores ordered entries whose values are opaque JSON objects. It does not
parse, normalize, or translate their provider payload. The resource records
format identity, project identity, opaque native session identity, and clean or
dirty status. Streams are ordered within a generation and subpath.

The runner may expose neutral store and lifecycle bindings to in process
adapters. These bind the HTTP resource and are not the cross process boundary.
Claude SDK types remain inside its adapter. Future OpenCode and LangGraph
adapters map their own serialization and checkpoint mechanisms to the same
capability without exposing provider types to core.

### 2. Exactly one context source per boot

One boot receives conversation context from native state or from the portable
preamble, never both. Memory and the bundle system prompt remain independent
inputs in either case.

Boot chooses context as follows.

1. Fake mode or a contribution without the capability selects portable
   context and does not contact the native resource.
2. A clean current generation whose format and project identities match the
   capability and whose main stream is nonempty selects native context. The
   adapter resumes its opaque native session identity. The portable transcript
   is neither loaded nor rendered.
3. An absent, dirty, or incompatible current generation selects the portable
   preamble exactly once for that boot. Curie creates a new dirty generation
   with a new native session identity. It never resumes, repairs, or appends a
   portable bootstrap to the old generation.
4. A clean compatible generation with an absent or empty main stream is corrupt
   and fails boot.
5. An unauthorized, timed out, malformed, or otherwise ambiguous native read
   fails boot. Curie does not switch to portable context after an uncertain
   read because doing so could inject both histories. The worker may reclaim
   the session, so recovery is automatic when the state API returns.
6. Native materialization failure after native selection also fails boot. It
   does not retry with portable context.

Format identity changes when compatibility is uncertain. Project identity uses
the same adapter rule as provider mirror writes. A mismatch selects a fresh
portable generation instead of migration.

### 3. Generation state machine

Every native capable turn follows this order.

1. Before the model receives a new turn, Curie durably marks the active
   generation dirty and the Claude adapter initializes its own per turn append
   uncertainty latch as certain. Failure prevents the model query. Steers
   within that open turn do not create another transition.
2. The Claude adapter wraps every `SessionStore.append` call. Any append
   exception, cancellation, or timeout latches uncertainty before the failure
   leaves the wrapper. A later retry cannot clear it.
3. The harness completes its native flush. A visible `MirrorErrorMessage` is
   telemetry only because the SDK may drop it when its message buffer is full.
4. After a would be successful result, Curie durably appends the portable
   `TurnRecord` projection.
5. Only when the native flush completed, the adapter latch remains certain, and
   the portable append succeeded may Curie durably mark the generation clean.
6. Curie emits a successful final only after the clean write succeeds.

A model failure, append uncertainty, portable append failure, clean transition
failure, interrupt, budget stop, abandoned stream, or process crash leaves the
generation dirty. Only the newest generation may change status. An older one
cannot become clean after fallback created a newer one.

The Claude SDK makes three append attempts in total, which means two retries.
It does not retry a timed out append because that append may still land. Curie
therefore preserves SDK entry identifiers as idempotency keys and enforces
uniqueness within a stream. Entries without an identifier append every time, as
required by the provider contract.

Clean means the last completed turn crossed the native flush, portable append,
and durable status boundary. A crash may leave native data behind the
projection, or a midturn flush may leave it ahead. Dirty status prevents either
partial generation from resuming.

### 4. Dedicated ordered persistence

Native checkpoints use a dedicated ordered durable log outside the pod. They do
not use `WorkflowStateEntry`, a pod volume, or one complete JSON array.

The persistence model has three concepts.

1. A generation binds one agent and thread to an ordinal, format identity,
   project identity, opaque native session identity, status, and timestamps.
2. A stream binds a generation to one normalized subpath and owns its next
   sequence number.
3. An entry stores one sequence, optional idempotency key, opaque JSON value,
   and timestamp.

Append serializes writes within one stream, assigns input order, ignores
repeated nonnull idempotency keys, and commits one accepted batch atomically.
Load preserves order. Unknown streams return no entries. Empty append creates
nothing.

The append endpoint bounds ingestion in three independent ways.

1. It streams the request body and rejects the first raw byte beyond the
   configured request limit before JSON parsing or database work.
2. It rejects a batch beyond the configured entry count before database work.
3. It rejects an opaque entry beyond the configured compact UTF 8 serialized
   byte limit before database work.

These limits bound one request and one entry, not total session growth.
Retention, capacity, deletion, export, compliance, and billing remain separate.

Numeric defaults come from observed mirror frames on runtime SDK 0.2.115 and
the workspace SDK version across ordinary turns, long tool output, and subagent
activity. Acceptance records largest observations, safety margin, and exact
defaults. Endpoint proof belongs to implementation after acceptance.

The existing broad agent scoped state token may authorize this resource. The
narrow app token is refused. This adds no process isolation from bundle code
that shares a sandbox and token posture with the runner.

### 5. Portable projection remains required

`TurnRecord` and its bounded readable preamble remain. Native payload is never
reconstructed from it or translated back into it.

The projection is the only context source for a harness without native resume.
It is also the fallback source when a capable harness has an absent, dirty, or
incompatible generation. For a native capable generation its append is part of
the clean transition, not best effort.

### 6. State classes and guarantees

This decision distinguishes three state classes.

1. Session context is the harness transcript and provider native metadata.
   Native resume restores it with more fidelity than the portable preamble.
2. Filesystem state includes the working tree, temporary files, home directory,
   and file checkpoint data. Native resume does not snapshot or restore it.
3. External side effects include writes to GitHub, Slack, connected tools, and
   other services. A transcript records what the harness observed. It does not
   prove whether a remote write committed or provide exactly once delivery.

Native context also does not restore prompt cache warmth, credentials, bundle
identity, repository state, or in process services. Existing side effect and
delivery rules remain authoritative.

## Consequences

1. A clean compatible restart preserves native prompts, tool calls, tool results, responses, and metadata rather than approximating them as a preamble.
2. Curie gains one neutral lifecycle while each harness owns its serialization.
3. The frozen ACI, wire artifacts, protocol version, and plugin format remain unchanged.
4. Existing threads start from their portable projection and create a native generation. Native resume begins only after a clean transition.
5. Native capable boot is less available during a state API outage than ADR 0029. Ambiguous reads may cause a reclaim loop until recovery.
6. Storage grows with native history. Retention and customer policy remain future work.
7. Native and portable stores may lag either way after a crash. Dirty fallback contains that uncertainty without reconciling side effects.
8. Logs may contain generation and format identity, never entries or user content.

## Post acceptance implementation criteria

1. A test fills the SDK message buffer so `MirrorErrorMessage` is dropped,
   forces `SessionStore.append` to fail, and proves the generation stays dirty.
2. Real endpoint tests prove each request, entry count, and entry size boundary
   succeeds and the first excess unit is rejected before database work.

## Alternatives considered

1. **Keep portable context only.** Rejected because it discards tool calls, tool results, and provider metadata when native mirroring is available.
2. **Expose provider SDK types in core.** Rejected because one provider format cannot be the platform contract or cross the ADR 0061 process boundary.
3. **Reuse capped `WorkflowStateEntry`.** Rejected because 64 KiB and 1 MiB caps
   cannot hold large entries and long lived ordered growth.
4. **Persist native files on a pod volume.** Rejected because suspend deletes
   the pod and provider disk layouts are not a Curie contract.
5. **Inject both contexts.** Rejected because duplication changes behavior,
   spends context twice, and hides checkpoint defects.
6. **Drop portable fallback.** Rejected because noncapable harnesses and dirty
   generations still need it, and operators need a readable projection.
7. **Use a provider managed runtime.** Rejected because Curie owns sandbox, deployment, bundle, governance, and observability responsibilities.
8. **Adopt LangGraph as the universal harness substrate.** Rejected because its graph lifecycle is one harness implementation, not a neutral replacement.
   Its adapter may map checkpoints and pending writes to this capability.

## Acceptance requirement

This ADR remains Draft and does not authorize implementation.

Acceptance requires all of the following.

1. The numeric ingress evidence and defaults described above are recorded in
   this decision.
2. Adversarial architecture review accepts the clean generation invariant, one
   source boot rule, neutral HTTP boundary, and deliberate startup failure
   posture.
3. A maintainer explicitly publishes the status as Accepted under
   [ADR 0085](0085-acceptance-not-implementation-authorizes-an-adr.md).

Only then may implementation begin. Acceptance approves the decision and does
not claim the store, adapter, tests, migration, or runtime proof exists.
