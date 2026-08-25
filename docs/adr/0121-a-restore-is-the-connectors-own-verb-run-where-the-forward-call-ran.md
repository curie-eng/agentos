# 121. A restore is the connector's own verb, run where the forward call ran

Date: 2026-08-25

Status: Draft

Answers the one decision
[ADR-0117](0117-a-tool-that-changes-the-world-reports-what-it-changed.md)
deliberately did not make. It changes no isolation boundary:
[ADR-0006](0006-security-rails-as-chart-defaults.md)'s rails and
[ADR-0008](0008-multi-tenancy.md)'s tenant compute stand as they are, and the
whole argument below is about not weakening them.

## Context

ADR-0117 shipped. Every side-effecting call is recorded with what it read, what
it left, and what it acted on; the API rules on an undo, refuses when the world
has moved, and refuses an actor who could not have permitted the forward change.
Then it returns, because **nothing in the platform can perform the restore it
just authorized.**

That gap is unchanged and re-verified for this ADR. Neither `apps/api` nor
`apps/worker` holds an MCP client — the `ClientSession` references in
[`apps/worker/src/curie_worker/reply_sink.py`](../../apps/worker/src/curie_worker/reply_sink.py)
and
[`apps/worker/src/curie_worker/runner_client.py`](../../apps/worker/src/curie_worker/runner_client.py)
are `aiohttp`, and every out-of-loop call either app makes is to the platform's
own API. MCP servers are handed to the SDK in
[`runner/src/curie_runner/adapter.py`](../../runner/src/curie_runner/adapter.py),
and the SDK owns those sessions.

ADR-0117 named three candidates and left them open. Building the ruling half
showed that they conflate two questions that have different answers, and that the
harder one was never really asked.

### The question ADR-0117's candidates skipped

All three candidates describe **who calls the connector**. None answers **what
call to make.**

A restore replays a recorded state: `{"spec": {"replicas": 3}}` onto a
Deployment. Turning that into `scale_deployment(replicas=3)` requires knowing
that this snapshot's `spec.replicas` is that tool's `replicas` argument. That
knowledge is per-tool and per-resource, and a platform that acquires it has built
the mapping DSL ADR-0117 rejects in its Alternatives — the one that "needs an
expression language, which is the commodity-engine category ADR-0007 exists to
keep out."

The connector already holds that knowledge. It produced the snapshot.

## Decision

**A restore is a verb the connector exposes, invoked by the runner inside the
sandbox the forward call ran in.**

1. **The connector restores its own resource.** A write connector that reports a
   prior state also accepts one back: a `restore` tool taking the recorded
   `target` and `prior_state` exactly as the ledger holds them, returning the
   same reply shape as any other write. The platform hands back the connector's
   own words and never interprets them.

   This is the same argument as ADR-0117 decision 1, applied to the return leg.
   The reply was the declaration because a manifest could disagree with what the
   tool actually returned; the restore is the connector's verb because a
   platform-derived call could disagree with what the tool actually does.

2. **The runner invokes it, in a sandbox, under the connector's own binding.**
   That is where the connector's credential, allowlist and network policy already
   apply, so **an undo can reach nothing the forward call could not.** Any other
   caller has to re-derive that ceiling, and a ceiling re-derived in a second
   place is one that drifts.

3. **The executor confirms completion to the ledger, and the ruling stops
   claiming it.** Today `undone_at` is set when the undo is authorized, because
   nothing reports back — a restore that never runs leaves a record saying it
   did. The ruling becomes a claim with an outcome: authorized, then confirmed or
   failed, with the failure on the record and on the receipt.

4. **A connector that reports a prior state and exposes no restore is a
   configuration error, surfaced at bundle validation.** Not at undo time, in
   front of a human who just pressed a button on a receipt that promised the
   action could be put back.

## Alternatives considered

- **An MCP client in the worker** (ADR-0117's first candidate). The smallest
  change, and the reason to reject it is decision 2. The worker is the control
  plane; giving it a direct client of tenant connectors makes the platform
  reachable to places ADR-0008 keeps separate, and the undo would then run under
  the control plane's network reachability rather than the sandbox's. An undo
  that can reach something the forward call could not is a privilege escalation
  wearing a safety feature's clothes.

- **A plain HTTP replay endpoint on each connector** (ADR-0117's third
  candidate). Avoids MCP, and pays for it twice: every connector author
  implements a second transport and its authentication, and the platform grows a
  second way to call a connector that the first way's allowlists do not cover.
  Decision 1 gets the same property — the connector deciding how to restore
  itself — without a new transport.

- **Deriving the restore call from the recorded arguments.** Rejected in
  ADR-0117 and rejected again here for a sharper reason: it is not merely
  unsafe, it is unavailable. `scale_deployment(replicas=10)` contains nothing
  that produces 3.

- **Letting the agent perform the restore as another turn.** Rejected in
  ADR-0117 decision 2 and unchanged: the undo would itself be a side-effecting
  turn needing its own record and its own approval, which regresses infinitely.

- **Executing nothing, and rendering the ruling as a suggested command.** Honest,
  and it is what the platform effectively does today. Rejected because it moves
  the one deterministic step in this design back onto a human at a keyboard,
  which is where the errors an undo exists to correct come from.

## Consequences

- **The runner must invoke an MCP tool outside the model loop, which it cannot do
  today.** This is the largest cost and it was ADR-0117's stated reason for
  deferring: the SDK owns the client sessions, so a second client inside the
  runner is new mechanism. It is a smaller mechanism than it looked, because it
  is needed for exactly one verb with a fixed argument shape and no model in the
  path.

- **A write connector that wants to be undoable now has two verbs to write, not
  one.** ADR-0117 already charged authors for returning JSON; this adds the
  return leg. The honest mitigation is that both are the same tool's read and
  write halves, and a connector that cannot restore can still report prose and be
  honestly irreversible.

- **A restore is a sandbox turn without a model**, which is a shape the platform
  does not have. Whether it reuses the existing sandbox lifecycle or gets a
  narrower one is an implementation question this ADR does not settle.

- **The snapshot travels further than it does today**, which sharpens the
  redaction question already open against ADR-0117: `redact.py` scopes itself to
  stdout and span attributes and explicitly leaves ACI frames alone. A prior
  state going back out to a connector is one more boundary that pass does not
  cover.

- **Whole-turn undo stays out of scope**, as in ADR-0117. Per-action remains the
  honest unit while a restore can fail independently per action.

## Out of scope

- **Retention and pruning of the ledger.** Unchanged from ADR-0117.
- **Undo from the console.** The receipt lands on the channel that asked.
- **Restoring anything the forward call did not report**, including side effects
  a connector performed but did not name. The ledger's account is the ceiling on
  what can be put back, and widening it is a new decision.
