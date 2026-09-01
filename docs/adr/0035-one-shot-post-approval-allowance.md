# 35. One-shot post-approval allowance at resume boot

Date: 2026-07-15

Status: Accepted

**Superseded in part by [ADR-0046](0046-converged-approval-gates-and-durable-provenance.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0046 supersedes the provenance discriminator this ADR used (the summary-prefix
sniff), replacing it with durable runner-authored provenance. The one-shot grant
lifecycle established below is otherwise unchanged.

**Partially superseded by [ADR-0106](0106-an-approver-is-an-authenticated-principal.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
ADR-0106 supersedes only this ADR's upstream non-requester guarantee. The
one-shot grant lifecycle and its other load-bearing properties remain in force.

Implements [#430](https://github.com/curie-eng/curie/issues/430).

## Context

The permission gate (#245, ADR-0010) marks tools approval-required and denies
them proactively through the runner's `can_use_tool` callback
(`runner/src/curie_runner/approval.py`): a gated call is blocked, the turn ends
`awaiting-approval`, the worker persists a durable `Approval` and suspends the
session (ADR-0003). When a human resolves the approval, the API enqueues a resume
turn (`apps/api/src/curie_api/resumequeue.py`) that walks the ordinary
consumer -> kernel -> claim path, and the platform-authored resume text tells the
model to "proceed with the approved action."

But the resume turn re-calls the gated tool and `can_use_tool` **denies it again**.
The approval-required tool set is rebuilt from durable config on every claim,
including the resume: `binding.boot_env()` re-injects
`CURIE_APPROVAL_REQUIRED_TOOLS` identically for fresh and resume claims
(`apps/worker/src/curie_worker/binding.py`, `kernel.py`), and the runner rebuilds
`ApprovalGate.required` from that env plus the bundle manifest's `approvalPolicy`
gates. A genuinely-approved action therefore cannot complete.

The stopgap in rc.1 was to make the gate the liftable config kind
(`approval_required_tools`, PATCHable) and have an operator PATCH the tool out at
approval time. A **manifest `approvalPolicy` gate is unliftable** without
redeploying the bundle, so the versioned, production-intended gate form could
never complete a post-approval action. The approval INTERFACE already flagged this:
"an approved tool call is still gated on retry after resume; a one-shot allowance
delivered at boot remains open follow-up work."

## Decision

Deliver a **tool-name-scoped, permission-gate-only, turn-scoped, single-use grant
at resume boot**, injected by the worker from durable state — never by the sandbox.

When the kernel builds the boot env for a claim whose `event_id` is the deterministic
resume id `approval-<id>-resolved` (`resumequeue.resume_event_id`) AND that approval
is `status='approved'` AND its `summary` is a permission-gate block (the
`summarize_tool_call` prefix `"Tool call awaiting approval: <tool> ..."`), the worker
sets `CURIE_APPROVAL_GRANT_TOOL=<approved-tool-name>` into the boot env. The runner's
`ApprovalGate` allows exactly one call to that tool on the boot turn (a new
`consume_grant(tool_name)`), then re-denies; `reset()` (start of every turn) expires
any unspent grant on the second turn.

Load-bearing properties:

- **Server-side, unspoofable.** The grant is derived by the worker from the durable
  `Approval` row, exactly like every other authorization decision (ADR-0010/0033/0034).
  A compromised sandbox cannot mint one; it can only *raise* a request. The
  non-requester guarantee is upstream — the authorizer denies self-approval before the
  status flips to `approved` (ADR-0034), so the worker only checks the status.
- **Permission-gate only, via a RESERVED summary namespace.** A **policy-gate** approval
  (`request_approval`, a business decision) has an arbitrary summary and receives NO
  grant. Granting one would hand the model a free bypass of any permission-gated tool the
  human never saw — a widening of the permission boundary. The discriminator is the
  `summarize_tool_call` prefix. Because a policy-gate summary is the model's own
  `request_approval(summary=...)` argument, that prefix is a **reserved namespace**: the
  runner guards model-authored summaries out of it (`guard_reserved_summary`, applied on
  the policy-gate capture in `translate.py`), so the model cannot forge a permission-gate
  summary and the worker's prefix check is an authoritative provenance signal rather than
  free-text inference. (Three reviewers flagged the naive "trust the summary prefix" form
  as forgeable; this reservation closes it without a contract change.)
- **Agent-bound.** The grant is delivered only when the approval's stored `agent_id`
  equals the agent currently resolved for the channel. A channel rebound to a different
  agent while an approval pends therefore cannot inject agent A's grant into agent B's
  runner; a mismatch or a NULL approval `agent_id` denies (fail-safe).
- **Tool-name-scoped (NOT argument-scoped).** The grant names exactly the approved tool.
  A second call to it, or any call to a *different* gated tool, is denied and re-pauses.
  It does **not** bind the tool arguments: on the resume turn the granted tool may be
  invoked with different arguments than the human saw in the summary. This is an accepted
  limitation — the resume text steers the model to the approved action, the grant is
  single-use, and the human approved *that tool*. Binding the exact arguments robustly
  requires the runner to persist a canonical input digest at block time, which crosses
  the frozen ACI `Final` contract; see Consequences / follow-up.
- **One-shot, re-armed.** Only the resume turn carries the resume `event_id`; every
  later mention has a different id and no grant env (re-armed). The `is_done(event_id)`
  guard means a completed resume turn never re-injects. The grant is valid for the boot
  turn only, so an adopted warm-pod follow-up mention cannot inherit it.

## Alternatives considered

- **A structured approved-tool field on the ACI `Final`.** Rejected as unnecessary.
  The approved tool name is already recoverable server-side from the persisted
  `Approval.summary`, and `binding` -> runner env is explicitly non-frozen (precedent:
  `CURIE_APPROVAL_REQUIRED_TOOLS`). Name-scoping needs no `packages/aci-protocol`
  (frozen-contract) change.
- **A name-agnostic grant** (allow the first gated call regardless of tool). Simpler but
  strictly less safe: it would let the resume turn run a different gated tool than the
  one approved, and (worse) fire on policy-gate approvals. Rejected in favour of scoping.
- **A DB lookup keyed by `conversation_id`** instead of the resume `event_id`. Rejected:
  it cannot cleanly express "exactly this resume, exactly once" without an extra
  consumed-flag column and a migration; the resume `event_id` already ties the grant to
  the specific resume turn and self-limits via the done-marker.

## Consequences

- A manifest `approvalPolicy`-gated tool completes exactly once after a genuine
  non-requester approval, with no operator PATCH and the gate re-armed afterward.
- **Known gap (fail-safe):** if the pod is *live* when the resume turn arrives — suspend
  failed (non-fatal) or a user mention resumed the thread first — `claim()` ADOPTS the
  live pod and the boot env is ignored, so the grant never reaches the runner. The
  approved action is re-denied and a second approval is created. The direction is
  fail-safe (the gate stays armed) and self-heals via re-approval; delivering a grant to
  an already-live pod would need a non-boot channel (steer/start_turn) and is out of scope.
- The worker parses two runner/API-owned string formats (the resume `event_id` and the
  permission-gate summary prefix). Both are pinned by tests that import the source
  helpers (`resume_event_id`, `summarize_tool_call`); the prefix pin also has a
  DB-independent unit assertion so it runs in every CI lane and a divergence fails the
  build rather than silently disabling the grant.
- **Follow-up (structured provenance).** The robust long-term form of the discriminator
  and of argument-binding is to persist explicit approval provenance on the `Approval`
  record — trigger kind (permission vs policy), the exact gated tool, and a canonical
  input digest — sourced authoritatively from the runner's `can_use_tool` block. That
  requires carrying the fields across the frozen ACI `Final` contract, which per AGENTS.md
  must land as its own reviewed, backward-compatible contract PR first. This ADR takes the
  non-frozen interim (reserved summary namespace + agent binding + tool-name scoping);
  the structured-provenance change is the recommended follow-up and would let the grant be
  argument-scoped, removing the tool-name-only limitation above.
