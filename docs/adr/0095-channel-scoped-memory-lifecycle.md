# 95. Channel-scoped memory: one surface-agnostic lifecycle for bootstrap, injection, and update

Date: 2026-08-04

Status: Draft

Extends [ADR-0025](0025-memory-port-and-first-loader.md) (per-agent memory over
the durable state store) and [ADR-0029](0029-conversation-history-port-and-first-loader.md)
(per-thread transcripts over the same store) with a third scope: the place a
conversation happens in. Designed against
[ADR-0020](0020-message-port-rendering-free-channel-interface.md) (the
rendering-free channel port) and [ADR-0012](0012-substrate-and-channel-agnostic-core.md)
(the runner never learns its channel), and sequenced behind
[ADR-0079](0079-inbound-triggers-as-a-new-event-kind.md) (inbound triggers as a
new event kind), whose `source` field and placeholder-less output path this
lifecycle builds on. Distinct from [ADR-0030](0030-proactive-within-episode-memory-agent.md)
(within-episode memory), which addresses behavioral decay inside one run; this
ADR addresses context continuity across runs in one place. Supersedes none.

## Context

Threaded work is the product's unit of execution: every mention boots a fresh
session (ADR-0003, stateless-first) whose context today is the agent's
cross-session lessons (ADR-0025), the thread's own transcript (ADR-0029), and
the bundle's `systemPrompt`. Nothing carries what the *channel* knows: the
running projects, conventions, decisions, and people that every thread in that
channel implicitly assumes. A new thread either lacks that context or would
have to re-read the channel backlog per turn, which does not scale in tokens
or latency. The fix used by every comparable product is a per-place memory
document: consume the channel once, keep a condensed durable note, inject it
into every thread.

Three facts about the current system shape the design:

1. **The seams already exist.** Runner boot composes preambles in one place
   (`runner/src/curie_runner/__main__.py`, `_compose_system_prompt`: agent
   memory, then conversation, then the bundle prompt). The worker mints
   memory and history refs in one place (`binding.py`, `boot_env`). Storage is
   the durable state store (`WorkflowStateEntry`: agent-scoped, namespaced,
   size-capped, CAS-versioned) with reserved namespaces `memory` and
   `transcript`. A consolidation pipeline (dedup, provenance union, replace)
   already exists in `runner/src/curie_runner/memory.py` but has no trigger;
   this lifecycle gives its shape a caller.

2. **"Per channel" and "per agent" are the same granularity today.** An agent
   binds exactly one Slack channel (`agents.slack_channel`, 1:1; the
   channel-ingress interface doc flags this as the largest remaining
   Slack-shaped surface, and multi-channel is deliberately deferred to epic
   #27). The scope key must nevertheless be the channel, not the agent, so
   that the stored artifacts survive into a multi-channel world; the limits
   the 1:1 binding still imposes are named in Consequences rather than
   papered over.

3. **There is no custom-instructions concept and no batch-job infrastructure.**
   The only model-facing instruction surface is the bundle `systemPrompt`,
   versioned and immutable per deployment, not operator-editable at runtime.
   The only periodic-work pattern is the API's in-process asyncio loops (the
   expiry sweeper's wait-first template); there is no CronJob anywhere.

External evidence (surveyed 2026-08-04) converges hard on a few points, and
the closest comparable product, Claude in Slack, is a direct precedent:

- **Memory is a curated note, not a transcript.** Every production system that
  ships to non-developers stores short, stable, human-readable prose and
  rejects transcript accumulation; the industry has visibly retreated from
  vector stores for this job (Letta's MemFS ships markdown with no vector
  index by default).
- **Two tiers with a hard budget.** A small always-injected core plus
  on-demand detail, with the injected tier's size enforced by the harness on
  every write (Claude Code: measure on write, warn near the limit, error over
  it), not left to model judgment. Context-rot measurements justify the cap.
- **Hybrid update triggers.** In-turn writes for explicit "remember this"
  (users expect immediacy), background consolidation for everything else
  (better recall, no hot-path latency). OpenAI and Letta independently shipped
  idle-time consolidation and both named it "dreaming".
- **Scope follows the access boundary.** "Memory follows places the same way
  access does": channel-scoped, never attached to an individual. Claude in
  Slack keeps memory by channel, shares public-channel memory upward, isolates
  private channels, and orphans a private channel's memory if it goes public
  (fail-closed over continuity).
- **Standing instructions outrank learned memory.** Claude in Slack layers
  operator-authored custom instructions above channel memory explicitly,
  because learned memory is the lowest-trust layer: nobody deliberately wrote
  it.
- **Memory poisoning is a studied attack surface.** The write channels this
  feature opens (agent-judged saves, backlog consumption, compaction) are
  three of the four channels in the published poisoning taxonomy
  (arXiv 2606.04329), on a surface where any channel member is an author.

## Decision

**Channel memory is a third scope over the durable state store, defined by a
surface-agnostic lifecycle with three platform-owned components (bootstrap,
injection, update) and one narrow obligation on each surface adapter.** Slack
is the first implementing surface; nothing below names Slack except its
adapter section.

### The surface obligation (the interface)

A surface adapter owes the platform exactly two things, and nothing else:

1. **A scope key.** Every inbound turn carries a stable, opaque
   `scope_key` identifying the place the conversation happens in (Slack: the
   channel id, already present as `reply_handle.channel`). The platform treats
   it as an opaque string, per ADR-0012. Each future surface defines its own
   mapping (email: likely the mailbox or recurring thread; decided in that
   surface's adapter ADR, not here).
2. **Optionally, a bootstrap trigger.** A surface that has a readable backlog
   may emit a one-time instigating event carrying a bounded backlog payload
   the adapter fetched. A surface with no backlog (email) emits nothing; its
   memory starts empty and accretes through the update path. Bootstrap is an
   optimization, not a requirement of the interface.

Injection, budget enforcement, and compaction are platform machinery and are
identical across surfaces.

### Storage

- A new reserved state-store namespace, `channel-memory`, joins `memory` and
  `transcript` in `RESERVED_NAMESPACES` (both the API and runner mirrors, per
  the warning in `routers/state.py`). Per scope it holds four keys:
  - the **document**: one curated markdown profile, the always-injected core.
    It is rewritten in place only by the bootstrap and compaction turns,
    always under compare-and-set;
  - the **notes log**: an append log of provenance-stamped records
    (ADR-0025's `MemoryRecord` shape) written by in-turn saves. Notes are
    injected after the document until compaction folds them in;
  - the **instructions**: an operator-authored plain-text note, editable at
    runtime through the API/console (extending the memory inspection UI of
    #267). **Writable with the platform key only**: the state router refuses
    writes to this key from `state`-scoped and app tokens, so no sandbox
    credential can author it. This restriction is enforced API-side and
    mirrored in the runner's reserved-namespace table like the namespace
    itself;
  - the **watermark**: the last-compacted position, its own machine-read key,
    never embedded in the prose document.
- Keys are scoped by `scope_key`, mirroring `transcript/<thread_key>`.
  Storage remains agent-scoped (`WorkflowStateEntry` has no channel table),
  so the scope key buys forward compatibility for the artifacts, not
  channel-portability across agents; see Consequences for the rebinding and
  multi-channel limits.
- The state store's per-value and per-namespace caps and scoped-token auth
  (ADR-0033) apply unchanged, and the namespace-count cap accounting (#852)
  gains one namespace. **Concurrency is explicit, not inherited**: CAS in the
  state store is opt-in, so this ADR requires it. Document rewrites carry
  `expected_version` and, on conflict, re-read and re-merge (or re-enqueue
  the compaction turn). In-turn saves only append to the notes log, never
  rewrite the document, so an explicit user save can never be discarded by a
  concurrent compaction; compaction clears only the notes it consumed, and a
  note landing mid-compaction survives to the next pass.

### Injection

- The worker mints a `CURIE_CHANNEL_MEMORY_REF` in `binding.boot_env`,
  alongside the existing memory and history refs. `boot_env` gains the scope
  key (the kernel already holds it as `reply_handle.channel`; today only
  `thread_key` is threaded through, so the signature widens by one opaque
  string).
- At runner boot the ref resolves like the other two (URL-shaped, scoped
  token, transient failure degrades to "no channel memory" without blocking
  boot), and `_compose_system_prompt` gains the channel block. Composition
  order: **custom instructions, channel memory (document then unfolded
  notes), agent memory, conversation preamble, bundle `systemPrompt`**, each
  under a labeled header. The authority ladder is stated in the rendered
  headers: the bundle prompt and custom instructions are authored guidance
  and outrank channel memory; channel memory is learned context, not
  instruction. Instructions and memory are read once at session boot; a
  running thread keeps the set it booted with.
- **The injected tier has a hard budget, enforced at the API state router on
  every write.** The document and notes log are measured against a fixed
  combined cap (Claude Code's 200-line / 25KB class of limit; exact numbers
  are implementation). Near the cap, the write succeeds and the response
  carries a warning instructing consolidation; over the cap, the write is
  refused with an error instructing a rewrite. Enforcement lives in the API,
  not the runner, so console edits and any future writer pass through the
  same gate; the cap is a platform decision, not a model judgment, because
  injection bloat degrades every thread in the channel at once.
- **Sibling paths are disposed of, not discovered**: eval runs stay hermetic
  (ADR-0051's fresh-conversation default), so an eval session gets no
  channel-memory ref, or a fresh throwaway scope when a case explicitly
  exercises memory. The `curie local` CLI stub channel mints a stub scope
  key and accretes memory like any scope, which is desirable for testing and
  confined to the local store. The fake tier remains plumbing-only and
  injects nothing.

### The memory turns and their kernel obligations

Bootstrap and compaction run as turns through the existing stream/kernel/
runner path, so they inherit routing, model selection, observability, and
cost attribution. They ride ADR-0079's queued-event extension (`source`
field, optional `placeholder_ts`) and add a second axis: a turn **kind**
(`interactive` today, plus `memory_bootstrap` and `memory_compaction`),
carried in the shared contract in `packages/`, never dispatcher-owned. That
is deliberately more than ADR-0079 grants, and the delta is named here as
sacred-kernel, single-owner work:

- **Output disposition: silent.** ADR-0079's placeholder-less path posts its
  reply to the channel; a memory turn must not. Its product is a state-store
  write, and the kernel discards its conversational output.
- **Transcript disposition: none.** Memory turns append nothing to any
  thread transcript (ADR-0029's write side is for conversational turns), and
  they carry a reserved conversation identity, not a user thread's
  `conversation_id`.
- **Ordering: locked per scope.** A memory turn takes the same per-thread
  order lock family as interactive turns (keyed by its reserved identity) so
  two compaction turns for one scope cannot interleave, and a compaction
  turn waits behind live interactive work rather than competing with it
  (ADR-0079's jobs-wait-for-idle semantics).
- **Approvals: inapplicable by construction.** Memory turns run with no
  side-effecting tools beyond the state API; they must not be able to reach
  an approval gate. If a memory turn requests an approval, that is a bug,
  and the kernel fails the turn rather than parking it.

### Bootstrap

- **Triggers, concretely.** The common case is control-plane, not Slack: an
  agent is bound to a channel when `agents.slack_channel` is set at
  create/update/deploy time, which emits no Slack event, so **the API emits
  the bootstrap trigger on binding**. The Slack-side event (the bot invited
  to a channel it is bound to) additionally requires the dispatcher to
  subscribe to `member_joined_channel`; the backlog fetch itself requires
  new read scopes (`channels:history`, `groups:history`, `pins:read`). The
  adapter fetches a bounded backlog (recent history plus pinned items) and
  enqueues the `memory_bootstrap` turn with the backlog as opaque text; the
  runner never calls a surface API (ADR-0012).
- **Idempotency lives in the turn, not the trigger.** The dispatcher has no
  state-store access and does not gain one; the bootstrap turn's first act
  is to check the scope's document and end silently if one exists (the same
  "channels that already have memory" suppression Claude in Slack
  documents). Duplicate triggers are therefore harmless.
- **The seed is conservative**: stable facts, named projects, conventions,
  and people, not a digest of the backlog. (Claude in Slack's join-time scan
  is documented as producing an intro post, not a memory write; the
  closest-comparable product chose caution here, and the poisoning
  taxonomy's compaction-channel attacks argue the same way.)

### Update

The lifecycle is a hybrid, matching the convergent industry pattern:

- **In-turn writes land immediately.** An explicit user ask ("remember for
  this channel: ...") and agent-initiated saves during work append
  provenance-stamped notes through the state API mid-session, durable and
  injected for the next thread the moment they land. Every write passes the
  budget gate above.
- **Compaction is a scheduled background turn, not new infrastructure.** An
  API-side scheduler loop (the expiry sweeper's wait-first template,
  including its multi-replica single-writer guard so two API replicas cannot
  double-enqueue) walks scopes on an interval (nightly by default) and, for
  each scope with new transcript activity or unfolded notes since its
  watermark, enqueues a `memory_compaction` turn. The turn reads the scope's
  transcripts since the watermark, the notes log, and the current document,
  rewrites the document in place (fold notes in, merge duplicates, correct
  stale facts, resolve conflicts newest-wins, union provenance, drop what
  the work has outgrown), clears the consumed notes, and advances the
  watermark, all under CAS as specified in Storage. A scope with no new
  activity is skipped, so cost is bounded by real usage, not channel count.

### Trust posture

- **Memory is context, never an enforcement boundary.** Nothing an agent must
  not do may rely on memory or custom instructions; approvals and policy
  gates ([ADR-0010](0010-approval-gates-and-human-in-the-loop.md),
  [ADR-0056](0056-operator-opt-in-for-policy-gate-grantability.md)) remain
  the enforcement surface, unaffected by memory content.
- Every memory write carries provenance back to the session and traces it was
  learned from; the document is human-readable and operator-editable through
  the console (the #267 surface), so audit and correction are first-class.
- The poisoning exposure is named and accepted with mitigations, not ignored:
  the bootstrap and compaction prompts distill toward stable facts under
  scope-limited write criteria, provenance identifies the contaminating
  session when poisoning is suspected, and the instructions layer (which no
  sandbox credential can write, enforced at the state router as specified in
  Storage) outranks the memory layer (which the agent can write).

### Validation gate (before maintainer acceptance)

Per ADR-0001, evidence before promotion. This Draft is accepted only with:
(1) an eval case proving a fact saved in thread A appears in a fresh thread
B's context in the same scope, and does not appear in another scope; (2) a
compaction case proving a named load-bearing fact survives the rewrite while
a planted stale fact is corrected; (3) unit tests on the budget gate at the
API enforcement point (warn near, refuse over) and on the CAS conflict path;
(4) one red-team case from the poisoning taxonomy (a backlog or thread
message that instructs the agent to write an instruction-shaped memory)
showing the write lands, is visible with provenance in the console, and
cannot reach the instructions key.

## Alternatives considered

- **Extend ADR-0025's per-agent memory instead of adding a scope.** Rejected:
  the two differ in authorship, lifecycle, and meaning. Agent memory is
  cross-place lessons the agent carries everywhere; channel memory is
  place-scoped shared context that must stay behind the place's access
  boundary. Conflating them is exactly what the 1:1 binding makes tempting
  today and what multi-channel (#27) would force apart with a migration
  later.
- **A retrieval layer (vector store or knowledge graph) instead of a curated
  document.** Rejected: the unit is one bounded document per channel, well
  within a context window; the surveyed industry trajectory is away from
  vector stores for this job; and the state store gives durability,
  scoping, caps, and audit for free. A retrieval tier over past transcripts
  can be added later without changing this decision.
- **Real-time-only updates (no compaction job).** Rejected: accretion without
  consolidation produces the documented staleness and bloat failure modes,
  and the budget gate alone would then force the model to consolidate during
  user-facing turns, moving maintenance cost onto the hot path.
- **Batch-only updates (no in-turn writes).** Rejected: users expect
  "remember this" to take effect immediately; every surveyed system honors
  explicit asks in-turn.
- **A full-digest bootstrap.** Rejected in favor of the conservative seed:
  the digest inflates the injected tier on day one, and backlog text is the
  least-trusted input in the poisoning taxonomy.
- **Dedicated compaction infrastructure (a CronJob, a job queue).** Rejected:
  compaction is LLM work and belongs in the runner path where routing,
  observability, and cost attribution already live; the only new piece is a
  small enqueue-only scheduler loop following an existing in-process
  template. This repeats #29's "no new execution machinery" principle.
- **Custom instructions inside the bundle `systemPrompt`.** Rejected: the
  bundle prompt is versioned with the deployment and owner-authored;
  operators and channel members need a runtime-editable, per-scope layer
  without a redeploy. The two compose; they are not the same knob.
- **A single memory artifact with in-place writes from the hot path.**
  Rejected: concurrent thread sessions and the compaction turn would race on
  one document, and a blind last-write-wins could silently discard an
  explicit user save. The document-plus-notes-log split keeps the hot path
  append-only and gives the document a single writer class.

## Consequences

- **Sequencing.** ADR-0079 is Accepted but its contract and kernel side are
  unimplemented (`QueuedTurn` today carries no `source`, and the placeholder
  is required). This ADR sequences behind that work and adds to it: the turn
  `kind` axis, the silent output path, the no-transcript rule, and the
  reserved conversation identity are kernel changes under the sacred-kernel
  single-owner review rule. Per the contract rules in `packages/`, a new
  kind enum is a breaking change (minor under 0.x); the optional fields are
  patches.
- `binding.boot_env` widens to carry the scope key; `RESERVED_NAMESPACES`
  grows by one entry in both mirrors, and the platform-key-only rule on the
  instructions key is a new, key-granular restriction the state router does
  not have today. The runner gains one preamble and one ref resolver, both
  copies of existing patterns.
- The Slack adapter gains a `member_joined_channel` subscription, three read
  scopes, and a backlog fetch path: a deliberate, confined widening of its
  "ack, dedupe, placeholder, enqueue" discipline. The new OAuth scopes are a
  reinstall/consent event for existing workspaces.
- Cost is one bootstrap turn per channel ever, plus one compaction turn per
  active scope per interval, skipped for idle scopes. Both are attributable
  per agent through existing observability.
- **Rebinding orphans memory, by choice.** Channel memory is stored under
  the agent that learned it; rebinding that agent to a different channel
  strands the old scope's artifacts (readable to operators, injected
  nowhere), and the new channel bootstraps fresh. This is the fail-closed
  choice the Claude in Slack private-to-public rule models; a platform-level
  memory move is future work, as is any handling of a Slack channel's own
  privacy transitions.
- **Multi-channel (#27) has a named prerequisite here.** The channel-memory
  namespace itself needs no migration (it is already keyed by scope), but
  transcript keys carry only the thread ts, so "this scope's transcripts
  since the watermark" is computable only while the 1:1 binding makes agent
  and scope coincide. Attributing transcripts to a scope key is #27 work.
- Custom instructions become a product concept for the first time. The v1
  editing surface is the state API and console; richer scoping (workspace or
  org tiers above the channel, as Claude in Slack layers them) is future
  work and does not change the storage or injection shape.
- The email surface (and any backlog-less surface) is served by the same
  lifecycle minus bootstrap: memory starts empty and accretes through
  in-turn writes and compaction. Its scope-key mapping is deliberately
  undecided here.
- Known limits, stated rather than discovered later: conflict resolution in
  a prose document is model rewrite plus human edit, with no algorithmic
  contradiction detector; forgetting happens only at compaction and the
  budget gate, with no time-based decay; per-thread transcripts remain the
  compaction job's input and stay subject to their own growth limitation
  (ADR-0029). ADR-0030's within-episode layer, if accepted, composes with
  this one and neither replaces the other.
