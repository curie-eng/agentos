# Approvals: human-in-the-loop turns

A Curie turn does not have to end in an answer. It can end **paused**, waiting for a
person to approve or reject something, and resume later with their decision. This page
is the one prose home for that plane: how a request is raised, where the card goes, who
is allowed to resolve it, and what your skill has to do when the decision comes back.

The narrative "why" behind the shape lives in the ADRs, chiefly
[ADR-0010](adr/0010-approval-gates-and-human-in-the-loop.md) (the approval primitive)
and [ADR-0034](adr/0034-approval-authorizers-resolve-membership-in-the-api.md) (one
authorizer over swappable approver sets). This page is the "how do I use it".

## The one-paragraph version

A run raises an approval. The worker persists a durable record, suspends the sandbox,
and posts a card to the channel the request's **route** is bound to. A human resolves
it; the platform authorizes that person **server-side**, wakes the suspended session
with a platform-authored turn carrying the decision, and the agent finishes the job it
started. Nothing about the decision is trusted from inside the sandbox, and nothing
holds a worker slot while a human thinks.

## Two ways a turn pauses

Both end the turn with the same status and share the entire downstream lifecycle
(`runner/src/curie_runner/approval.py`).

**Policy gate.** The agent decides something needs sign-off and calls the built-in
`request_approval` tool, which the SDK exposes as `mcp__curie__request_approval`. It
takes a one-line `summary` and an optional `route`. The call executes nothing; it marks
the turn. Your skill should call it, then say the request is pending and end the turn.

**Permission gate.** Configuration marks a tool approval-required, and the runner denies
the call through the SDK's `can_use_tool` callback before it runs. The denied call never
executes. Two sources name gated tools and they are unioned, never subtracted: the
bundle manifest's `approvalPolicy`, and the `CURIE_APPROVAL_REQUIRED_TOOLS` operator
override.

## Declaring routes in the bundle

A **route** is a name for a class of decision, declared in the bundle so it is versioned
with the agent. Today a route is declared as part of a gate
(`packages/plugin-format/src/plugin_format/models.py`):

```json
{
  "name": "deal-desk",
  "version": "0.2.0",
  "approvalPolicy": {
    "gates": [
      { "gate": "mcp__plugin_deal-desk_core__post_invoice", "route": "finance" }
    ]
  }
}
```

`gate` names the tool the runner intercepts; `route` names the approval route the
platform binds to a channel per deployment. Declaring a route this way means the runner
validates the model's `route` argument against it in-turn: an unknown route comes back
as a tool error naming the valid ones, so the model can retry inside the same turn
instead of burning it.

View what a bundle declares, offline and with no credential:

```bash
curie skill approvals
```

## Binding a route to a workspace

Declaring a route says nothing about where it goes. The binding is **operator-owned**,
per agent, and read fresh at resolve time so revoking an approver takes effect on the
next click rather than the next restart:

```json
{
  "approval_routes": {
    "finance": {
      "channel": "C0BBG383GLF",
      "approvers": { "group": "S0123ABCD" }
    }
  }
}
```

Two independent axes, and keeping them separate is the point:

- **`channel` is WHERE the card posts.**
- **`approvers` is WHO may act on it.** Omit it and the card channel's members are the
  approvers, which is the zero-setup default.

A route the bundle names but the agent never bound is **escalated to a human**, not
posted to the requesting channel. Silently widening a request to whoever happens to be
in the requesting channel is exactly the failure the gate exists to prevent, so the
platform refuses rather than guesses.

> There is no `curie` flag for writing this binding yet; it is a `PATCH /agents/{id}`
> today. Adding the verb is tracked in
> [#1052](https://github.com/curie-eng/curie/issues/1052).

## Who may resolve

One authorizer decides, and the swappable part behind it is the approver set
(`apps/api/src/curie_api/authorizer.py`, `apps/api/src/curie_api/approvers.py`). The
precedence is fixed: `users` beats `group` beats channel membership.

| Declared | Set | Lookup | Does the click channel matter? |
|---|---|---|---|
| nothing | `SlackChannelMembers` | none, the click's channel is the proof | yes, it is the only test |
| `approvers.group: S...` | `SlackUserGroupMembers` | Slack `usergroups.users.list` | no |
| `approvers.users: [U...]` | `ExplicitUsers` | none, pure config | no |

Because `users` and `group` ignore the click channel entirely, a card can sit in a room
everyone can read while only a narrow set may act. That unfusing of *where* from *who*
is the whole subject of ADR-0034.

Three properties worth knowing before you design around this:

- **Self-approval is refused under every set.** The author of the turn that raised the
  request may not resolve it, from any channel. The check runs before any set is
  consulted, so no set can skip it. Testing an approval flow therefore needs a second
  actor.
- **The buttons are visible to everyone in the channel.** Slack cannot hide a button per
  user, so authorization is enforced when the click arrives, not by hiding the control.
  A refused click gets a private, reasoned refusal.
- **A lookup that fails denies.** A group-bound route with no bot token, or a Slack
  outage, reports "could not verify" and refuses. It never falls back to channel
  membership, because that would silently widen the set an operator narrowed.

Resolving a group-bound route needs `SLACK_BOT_TOKEN` on the API with the
`usergroups:read` scope (`apps/api/src/curie_api/slack_usergroups.py`). Without it, such
a route fails closed.

## What your skill must handle on resume

This is the part nothing in the command tree tells you. When an approval is resolved,
the platform enqueues a normal turn on the runs stream whose text it authored itself
(`apps/api/src/curie_api/resumequeue.py`):

```text
[approval resolved] The request "<summary>" was approved by <U123>. Note: <text>.
Continue the task accordingly: proceed with the approved action, or acknowledge the
rejection and stop.
```

An expiry produces the sibling `[approval expired]` turn, which states that nobody
decided and the gated action must not be performed.

**Your `SKILL.md` must have an instruction for those prefixes.** A skill that does not
recognize them will treat the resume as an unrelated user message and the verdict is
silently dropped. Give the skill a section that says what to do on each, including for
a rejection.

The reply streams back into the **same message** the "awaiting approval" notice was left
on, because the resume turn replays the original turn's reply handle.

## Driving it from the CLI

Approval records live in the platform, so these verbs answer at the `local` and
`cluster` tiers. At `skill` the tier declines with a reason rather than erroring, since
a bare runner keeps no durable store
([ADR-0077](adr/0077-skill-tier-durable-approvals-stay-unavailable.md)).

```bash
# what is pending, with each record's id and bound channel
curie local approvals my-agent --list

# settle one as a named actor (must not be the requester)
curie local approvals my-agent --resolve <id> --as U0MANAGER --actor-channel C0BBG383GLF --note "approved for Q3"

# reject instead
curie local approvals my-agent --resolve <id> --as U0MANAGER --reject --note "discount too deep"

# which tools are gated behind approval
curie local approvals my-agent
```

Resolution is **once-only**: the first authorized resolver wins the compare-and-set, and
a later one is told who won rather than overwriting the decision.

`--actor-channel` is what proves channel membership for the default approver set. The
channel to pass is the one `--list` reports for that record. A route that declares
`approvers.users` or `approvers.group` ignores it, so passing it there is harmless.

## Operational guarantees

- **A paused turn holds nothing.** The queue entry is acknowledged when the turn
  suspends; the resolution arrives later as its own queued turn. No stream entry, thread
  lock, or worker slot is held across a human's lunch break.
- **A suspended sandbox is deleted, not frozen**
  ([ADR-0003](adr/0003-stateless-first-rehydrate-on-resume.md)). Resume cold-creates a
  fresh one and rehydrates from history. Never design a feature that needs in-process
  state to survive a pause.
- **Unresolved approvals expire.** A sweeper settles records nobody answered and wakes
  the session down its timeout branch (`apps/api/src/curie_api/sweeper.py`), and a
  reconciler re-drives resolutions whose resume turn never landed
  (`apps/api/src/curie_api/resumereconciler.py`).
- **Every attempt is audited.** `GET /approvals/{id}/audit` returns each resolution
  attempt with the authorizer that decided and the membership evidence it decided on,
  including refusals.

## Common failures

| Symptom | Cause and fix |
|---|---|
| `403 self-approval is blocked` | You are the author of the turn that raised it. Resolve as a different actor. |
| `403 you are not an approver` | The route's set does not admit that actor from that channel. Pass the `--actor-channel` the record reports, or check the `approvers` block. |
| `403 could not verify approvers` | A group lookup failed or the approvers block does not parse. Check `SLACK_BOT_TOKEN` and the `usergroups:read` scope; this never falls back to channel membership. |
| `409 already resolved by ...` | Someone else won the claim. The decision stands. |
| `410 expired` | The record passed its deadline. The session was already woken down its timeout branch. |
| Agent says a request is pending, no card anywhere | The named route is not bound for this agent, so the turn escalated instead of posting. Add the binding. |
| Card resolved, but the agent never continued | The skill has no instruction for the `[approval resolved]` prefix. |

## Where the code is

| Concern | Path |
|---|---|
| Raising a request, and the tool gate | `runner/src/curie_runner/approval.py` |
| Pausing, routing the card, suspending | `apps/worker/src/curie_worker/kernel.py` |
| Remembering the card so an expiry can disable it | `apps/worker/src/curie_worker/approval_cards.py` |
| Click to resolve, and rendering the verdict | `apps/dispatcher/src/curie_dispatcher/approval_actions.py` |
| The resolve endpoint, claim, and audit | `apps/api/src/curie_api/routers/approvals.py` |
| Who may resolve | `apps/api/src/curie_api/authorizer.py`, `apps/api/src/curie_api/approvers.py`, `apps/api/src/curie_api/slack_approvers.py` |
| The resume turn | `apps/api/src/curie_api/resumequeue.py` |
| The manifest shape | `packages/plugin-format/src/plugin_format/models.py` |
