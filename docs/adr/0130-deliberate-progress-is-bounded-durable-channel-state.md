# 130. Deliberate progress is bounded durable channel state, not answer streaming

Date: 2026-08-27

Status: Accepted

Accepted with explicit maintainer approval on 2026-08-27, before implementation.
This extends [ADR-0020](0020-message-port-rendering-free-channel-interface.md)'s
semantic channel interface and
[ADR-0096](0096-port-adapters-are-deployed-services.md)'s neutral reply wire. It
does not change the frozen ACI session protocol or plugin format.

## Context

Curie can already tell a channel that a turn is alive, update an addressable
reply, post a platform-owned message, settle an approval card, steer a live turn,
and recover terminal delivery from a durable outbox. Those mechanisms do not yet
define a product contract for deliberate in-turn progress.

The distinctions matter. `turn.status` is a best-effort liveness caption, not a
durable task record. `reply.update` primarily carries streamed or final answer
text and approval settlement. `reply.post` creates an approval card but has no
generic stable delivery identity. Ordinary tool notes describe model activity;
they are not permission to expose raw tool chatter, hidden reasoning, or an
unbounded event log to a user.

The executable mechanism spike in
[the SRE progress report](../../.projects/spikes/sre-progress-contract.md) drove
the real worker kernel, runner HTTP client, sandbox substrate, thread routing,
steering, completion outbox, and a real disposable Valkey while mocking only the
model, Slack, GitHub, and Kubernetes. It proved all of the following without a
change to `packages/aci-protocol` or `packages/plugin-format`:

- a stable progress identity can own idempotent mutable state;
- a durable reservation can bound milestone posts;
- a steer sent after visible progress reaches the live turn;
- approval remains a separate, idempotent card;
- the existing completion outbox still owns one canonical final reply.

It also found two gaps that static composition did not answer. First, a crash
after a generic `reply.post` succeeds but before the worker acknowledges it can
post the same milestone twice because `ReplyPost` has no delivery identity.
Second, a placeholderless steer currently adds a bot-authored folded-receipt post
before the final answer, defeating the intended post-once Slack experience even
though there is technically only one final answer.

The remaining decision is therefore not whether every internal event can be
rendered. It is which small, intentional subset becomes durable user-facing
progress, how that subset is bounded, and where its delivery identity lives.

## Decision

### 1. Deliberate progress is a platform-owned semantic operation

Curie exposes a platform-owned `curie_progress` operation with a closed,
versioned input model. The operation is available to the model as a built-in
tool, but its semantics belong to the platform, not to a bundle and not to a
channel adapter.

The input identifies a progress record, an idempotent update, one state, a short
human-readable summary, and optionally one milestone class. The initial state
set is:

- `queued`
- `investigating`
- `awaiting-approval`
- `preparing-workspace`
- `testing`
- `publishing`
- `complete`
- `failed`
- `cancelled`

The model cannot provide a channel kind, channel address, reply reference,
transport endpoint, credential, adapter payload, delivery identifier, or
milestone budget. The worker resolves the already-authorized reply target and
owns those values.

Ordinary tool notes remain internal activity telemetry. A tool call does not
become progress merely because it happened, and the implementation must not
parse JSON or another opaque subprotocol from `ToolNote.text`. The progress tool
handler sends its validated command through a scoped platform-internal ingress
to a worker-owned progress coordinator. The existing ACI stream continues to
carry its existing events unchanged.

### 2. One logical turn chain owns one mutable progress card

A stable `progress_id` identifies one progress record for the logical turn
chain. The chain includes a suspend and resume across an approval; resuming an
approval does not mint a new progress budget. A genuinely new turn after the
prior chain completes gets a new progress record.

Every state change carries an idempotency identity. Repeating the same accepted
update is a no-op. A stale update cannot move the record backward or overwrite a
newer revision. Terminal states are monotonic: after `complete`, `failed`, or
`cancelled`, a late update cannot reopen the record.

The channel renders that record as one mutable task card when it supports rich
cards and addressable updates. A lower-capability adapter renders the same
semantic message through its mandatory text fallback. The card remains in the
conversation after completion, compactly marked with its terminal state; it is
not replaced by the final answer.

### 3. Durable milestones are useful and capped at three

Each logical turn chain may create at most three durable milestone replies. The
initial milestone classes are:

1. intake or material evidence acquired;
2. material hypothesis or scope change;
3. verification result.

The classes describe why a durable interruption is warranted, not a required
sequence. A chain may use fewer than three, may omit a class, and may use one
class more than once only while budget remains. The worker atomically reserves a
slot before delivery; concurrency, retry, approval resume, or process restart
cannot reset or exceed the cap.

Approval cards and the canonical final reply do not consume milestone slots.
`awaiting-approval` updates the progress card, while the existing approval
contract remains the one actionable durable approval message. Progress must not
duplicate the approval request as a milestone.

### 4. Progress delivery has stable identity at the neutral reply boundary

Durable progress updates and milestone posts carry a platform-minted
`delivery_id` through the neutral reply wire. `delivery_id` is optional for
existing reply forms and mandatory for progress delivery. Adding it requires a
minor reply-wire version increment and regenerated channel-protocol schema and
compatibility fixtures; the change does not touch either frozen package.

Adapters treat `delivery_id` as the idempotency key for the externally visible
operation. Slack maps it to `client_msg_id` for a new post, matching the existing
approval UUID precedent, and deduplicates an ambiguous crash retry. An update
targets the adapter-minted opaque `reply_ref` and retains the same delivery
identity on retry. Non-Slack adapter conformance tests must preserve the identity
and prove their stated idempotency quality.

The worker persists progress state, milestone reservations, and pending delivery
before calling the adapter. It acknowledges delivery only after the adapter
answers. Recovery retries the same pending record with the same `delivery_id`;
it never creates a replacement identity for an ambiguous attempt.

### 5. Progress never becomes a second answer

Progress and answer text are separate semantic objects. Progress contains short
task state and milestone summaries, never hidden reasoning, raw tool output,
secrets, token-by-token deltas, or a draft answer.

The existing answer path and completion outbox remain the sole owner of the
canonical final reply and terminal `turn.completed` delivery. A successful
placeholderless steer is acknowledged by updating the mutable progress card or
silently. It must not create the current folded-receipt message. A steer that
arrives after the chain is terminal follows the existing finish-race behavior as
a new turn; it cannot mutate the closed progress record.

## Consequences

- Slack gets one mutable task card, zero to three durable milestone replies,
  separate approval cards when needed, and one separate canonical final answer.
  The maximum number of progress-authored Slack messages is therefore four: one
  card plus three milestones.
- Quiet turns remain quiet. The cap is a ceiling, not a quota, and ordinary tool
  activity never earns a message by itself.
- The progress coordinator is durable worker state with compare-and-set and
  outbox behavior. A process-local counter or adapter-local task state would
  rearm after restart and is not conforming.
- `packages/channel-protocol` changes additively and versions its closed wire.
  `packages/aci-protocol` and `packages/plugin-format` remain unchanged.
- A scoped internal progress ingress is new platform plumbing. It must
  authenticate the running turn, reject cross-turn or cross-tenant updates, and
  expose no channel credentials or routing addresses to the sandbox or model.
- Cancellation and failure terminate the card even when no final answer can be
  delivered. Recovery may retry delivery, but it cannot reopen the semantic
  task.
- The implementation requires a real disposable Slack proof in addition to
  durable local regressions. That proof must show three meaningful milestones
  before completion, duplicate suppression across an ambiguous retry, steering
  after a visible milestone, terminal card retention, and exactly one final
  answer.
- Acceptance authorizes implementation; it does not claim that implementation
  exists. The implementing issue and pull request must link this ADR and name the
  progress ingress, worker coordinator, neutral reply-wire change, and Slack
  adapter path that realize it.

## Alternatives considered

1. **Use only `turn.status`.** Rejected because it is best-effort liveness with
   no durable identity, history, milestone budget, or recovery contract.
2. **Post milestones only.** Rejected because every state change becomes thread
   noise and there is no single current task state to inspect or steer against.
3. **Render only a mutable card.** Rejected because material evidence,
   hypothesis, and verification transitions become easy to miss in a long
   thread. Three durable interruptions are a bounded compromise.
4. **Emit every tool note or model event.** Rejected because internal activity is
   neither a product milestone nor safe user-facing content. It leaks noise and
   can leak reasoning, tool output, or secrets.
5. **Encode progress as JSON in `ToolNote.text`.** Rejected because it creates an
   undocumented protocol inside a human-readable field and makes ordinary tool
   telemetry load-bearing. The spike used that shape only as disposable
   stimulus.
6. **Add progress events to the frozen ACI.** Rejected because the spike proved
   that the product behavior can compose outside that cross-language session
   contract. ACI carries runner-session behavior; durable channel presentation
   belongs to the platform progress ingress and neutral reply boundary.
7. **Let each channel invent progress semantics.** Rejected by ADR-0020. State,
   milestone class, cap, and delivery identity are semantic; only rendering is
   adapter-owned.
8. **Reset the cap after approval resume.** Rejected because a human sees one
   logical task. A suspend/resume implementation detail must not rearm its spam
   budget.
9. **Delete the card after the final answer.** Rejected because it erases the
   compact task history that made steering and failure diagnosis legible.
10. **Post a folded-receipt message for every steer.** Rejected because it
    violates the post-once experience and competes with meaningful milestones.
