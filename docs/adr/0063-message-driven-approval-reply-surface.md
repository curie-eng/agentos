# 63. Message-driven approval reply surface

Date: 2026-07-21
Status: Accepted

**Deferral superseded by [ADR-0078](0078-approval-cards-over-connected-transport.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
this ADR's Decision defers the connected Slack transport as the approval surface
("not (yet) the connected Slack transport", its Alternative 1, "Follow-up A").
0078 adopts it. The zero-Slack keep-alive decision below is unaffected.

## Context

`curie local message` and `curie cluster message` mint an in-process
`SlackStub` (`cli/src/chat.rs` `SlackStub::start`) and stamp its URL as the
turn's `reply_handle.endpoint`. When the turn is approval-gated, the worker
persists that endpoint on the durable `Approval` record as `reply_endpoint`
(`apps/api/src/curie_api/resumequeue.py`), suspends the turn, and posts the
approval card (`apps/worker/src/curie_worker/kernel.py`, around lines
937-953). The CLI then returns `Outcome::AwaitingApproval` and exits, which
drops the `SlackStub` and aborts its HTTP listener.

When a human later resolves the approval, the resume turn
(`resumequeue.build_resume_turn`) replays that now-dead endpoint. With no
connected Slack transport configured, the reply is swallowed by the
best-effort fallback landed in #708 (`slack_sink.py`, ACK not dead-letter) and
never reaches the operator. Issue #529 added the `warn_approval_will_strand`
notice documenting this as expected behavior; #708 (referencing the OPEN
parent, and #505/ADR-0039's bounded-delivery/dead-letter mechanism) made the
undelivered case non-fatal to the worker but did not make the reply arrive.
Issue #766 is the acute complaint: the resumed reply for a `message`-driven
approval dead-letters instead of reaching the operator who is waiting on it.

ADR-0020 established the message port as a rendering-free channel interface
and treated the CLI stub as one adapter among several, implicitly assuming it
is a throwaway per-turn fixture. This ADR qualifies that assumption for the
approval-pending case: a throwaway stub cannot be the reply surface for a
turn that pauses, because the receiver is gone before the pause resolves.

## Decision

**The message-driven approval reply surface is the CLI stub kept alive for
the approval-pending window**, not a new persistent server and not (yet) the
connected Slack transport.

On `Outcome::AwaitingApproval`, `local message` and `cluster message` do not
exit. They keep the same `SlackStub` instance running, extract the approval
id from the worker's placeholder notice, print a resolve hint (`curie
<tier> approvals <agent> --resolve <id> --as <user> --actor-channel
<channel>`), and enter a second bounded
wait for the resumed reply on the same placeholder.

That second wait reuses the SAME ack-based completion signal as the original
turn, rather than polling approval status over HTTP. Resolving an approval
does not open a bespoke channel: the platform API appends the resume turn
onto the same `curie:runs` stream the CLI already enqueued onto, under the
deterministic event id `approval-<id>-resolved`
(`resumequeue.resume_event_id`), replaying the original turn's placeholder
and this stub's reply endpoint. So the CLI scans that stream (over the Valkey
connection it already holds for the enqueue) for the resume entry, and once
it appears delegates to the existing `await_reply` on that entry's stream id.
Completion is therefore the worker's XACK of the resume entry -- the turn
having FINALIZED -- exactly as for the original turn. When the resumed reply
arrives at the still-live stub, the CLI prints it and exits 0. On timeout,
the CLI exits with the existing transient (retryable) exit class, leaving the
durable `Approval` record pending and resolvable later.

Reusing the ack signal rather than a `GET /approvals/{id}` status poll is
load-bearing, not incidental. A status poll observes only that the approval
left `pending`; it carries no signal that the resumed turn has finished, so
the first placeholder edit after resolution -- which may be a booting notice
or a partial streaming edit -- would be printed as the final answer. It also
needs the API reachable for the whole pending window, which at cluster tier
would mean holding a second `kubectl port-forward` open across the wait. The
stream scan needs neither: it reuses the connection the enqueue already
opened and inherits `await_reply`'s finalization semantics for free.

This is deliberately the CLI-side half of a two-sided contract. Durability of
the approval itself lives in the Postgres `Approval` record, per ADR-0010,
which explicitly rejected holding pending approval state in memory
(alternative 4 in that ADR: "hold pending state in memory on the live
sandbox"). The CLI keep-alive does not touch that decision or reintroduce
in-memory approval state; it keeps alive only the reply *receiver* -- the
stub that the resume turn's HTTP call lands on -- never the approval record
or its resolution. No approval semantics change: resolution remains an
explicit `approvals --resolve` call or a real card click, authorized
server-side per ADR-0034/ADR-0035; the CLI wait loop only reads.

Connected real Slack is the eventual durable, clickable-card reply surface
for this flow, but it is explicitly deferred (see Consequences). Nothing in
this decision blocks that path; it only fixes the zero-Slack case that #766
reports as broken today.

## Alternatives considered

1. **Route the card and resumed reply through the connected Slack endpoint
   (Option 2 in the #766/#708 framing).** This is the eventual durable
   surface and is not rejected, only deferred. It requires the CLI to detect
   a connected transport, diverge into a no-stub code path, rework the
   `cluster message` `--force-wire` hijack guard, and be verified against
   real Slack. It also requires overturning a deliberate, pinned design from
   #451: a requesting-channel card rides the trigger's transport
   (`kernel.py` around lines 940-945, pinned by
   `apps/worker/tests/kernel/test_approval_lifecycle.py` lines 522-544). A
   late resume reply to real Slack also cannot `chat.update` the synthetic
   placeholder timestamp the offline stub uses, since that timestamp never
   existed in real Slack, and must post top-level instead. Both are
   sacred-kernel/`slack_sink.py` design changes that deserve their own ADR,
   not a passenger on this one. Deferred to Follow-up A.
2. **A new persistent/daemon reply server, independent of the CLI process.**
   Rejected as the naive-correct answer for this slice: the stub already
   kept alive for the pending window is a sufficient durable-enough reply
   surface for the offline `message` loop, and a standalone server is
   materially more infrastructure for the same result.
3. **Hold pending approval state in memory instead of relying on the
   Postgres `Approval` record.** Already rejected by ADR-0010 (alternative
   4) for the platform generally; this ADR does not revisit it. The CLI
   keep-alive is compatible with that rejection because it never holds
   approval state, only the reply receiver.
4. **Poll `GET /approvals/{id}` until `status != pending`, then take the next
   placeholder edit as the resumed reply.** Rejected. This was the initial
   design and is the more obvious one -- it reuses a client the CLI already
   builds -- but approval status is the wrong signal. It reports that a human
   decided, which says nothing about whether the resumed TURN has finished, so
   the first edit after resolution (a booting notice, or a partial streaming
   edit) gets printed as the final answer. Guarding that with "wait for the
   edit AFTER resolution" only moves the race, because a reply landing between
   two polls is indistinguishable from one landing before them. It also keeps
   the API on the critical path for the whole pending window, which at cluster
   tier means holding a second `kubectl port-forward` open across the wait. The
   stream scan chosen above avoids all of this by construction: it reuses the
   Valkey connection the enqueue already opened, and inherits `await_reply`'s
   XACK-means-finalized semantics rather than approximating them.
5. **A lighter completion signal still (text-diff on the placeholder).**
   Rejected. Comparing successive placeholder text to guess when the answer
   stopped changing is a timing heuristic with no terminal signal, and it
   cannot distinguish a slow model from a finished one.

## Consequences

- The `message` CLI verb's reply-endpoint lifetime changes from
  "throwaway per-turn" to "alive across the approval-pending window,"
  qualifying ADR-0020's implicit one-throwaway-stub assumption. No other
  consumer of `SlackStub`/`await_reply` (`curie chat`) is affected; its
  outcome handling is unchanged.
- No kernel, worker, API, or dispatcher change. The endpoint value the
  worker already persists on the `Approval` record was already correct; it
  only needed to be *alive* when the resume posts. This keeps the change
  entirely within `cli/src`.
- The offline reply-surface fix does not eliminate the dead-letter case, it
  shrinks the window in which it occurs. If the CLI's wait times out or the
  process exits before resolution, a later resolve still falls through to
  the pre-existing #708 best-effort swallow (transcript-only ACK, not a
  worker dead-letter). This is unregressed and deliberate; the resume wait's
  finalization-gated completion and its never-resolved timeout are covered by
  the Valkey-backed integration test `cli/tests/resume_wait.rs` (skipped when
  no Valkey is reachable, like the other `chat_enqueue.rs` seam tests), which
  drives a real resume entry through `await_resume` and asserts a finalized
  reply, and drives the no-resume case and asserts the timeout (never a false
  reply). If the resumed turn parks on a NEW gate, the CLI keeps waiting on the
  nested approval's resume entry rather than exiting and re-stranding it.
- The awaiting/pending output (human and `--json`) must never claim a
  durable, clickable card exists when no connected Slack transport does.
  Offline, the durable resolution surface is the `Approval` record plus
  `curie <tier> approvals <agent> --resolve`, and that is what the CLI's
  hint points at.
- **Follow-up A -- "Route message-driven approval cards + resumed reply
  through the connected Slack transport."** Filed as #770. Implements
  alternative 1 above: when `message` detects a connected transport, skip
  minting the stub so the card and resume reply post over the worker's
  default transport to a real Slack thread, giving an actually clickable
  card. Requires overturning the pinned #451 requesting-channel design and
  real-Slack E2E verification.
- **Follow-up B -- "SKILL-tier `approvals --list`/`--resolve` parity with
  local/cluster."** Filed as #771. `skill message` talks directly to the
  runner ACI with no dispatcher, Valkey, worker, or resume machinery, so
  this decision's keep-alive mechanism has nothing to keep alive at that
  tier. SKILL-tier durable approvals is a distinct, larger change and stays
  out of scope here.
