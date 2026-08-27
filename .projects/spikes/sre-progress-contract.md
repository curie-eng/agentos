# SRE Deliberate Progress Contract Mechanism Spike

**Date:** 2026-08-26

**Status:** completed

**Decision follow-up:** the recommended rendering and lifecycle semantics were
accepted in [ADR-0130](../../docs/adr/0130-deliberate-progress-is-bounded-durable-channel-state.md)
on 2026-08-27.

**Release-train base:** `origin/next` at `0f20d32cf523293ad72d4cb8b3486709251af860`

**Spike branch:** `task/sre-progress-contract-spike`

## Decision

**YES: Curie has a no-ACI implementation path.** Deliberate in-turn progress can use a
stable progress ID, idempotent state updates, a configured cap on durable milestone
replies, an approval card, steering after a visible milestone, and one canonical final
answer without changing `packages/aci-protocol` or `packages/plugin-format`.

That is not the same as saying today's generic reply wire is sufficient unchanged. The
first concrete gap is `packages/channel-protocol/src/channel_protocol/reply.py`:
`ReplyPost` and `ReplyUpdate` do not carry a stable delivery identity. The shipped
approval path closes this gap only for `ConfirmIntent` by mapping its durable approval ID
to Slack `client_msg_id`; generic milestone posts have no equivalent. An additive
`delivery_id` on durable progress deliveries, plus adapter dedupe, is required. This is a
channel reply contract change, not an ACI or plugin-format change, and neither frozen
package was modified by the spike.

Do not ship the spike's temporary JSON-in-`ToolNote.text` stimulus as an implicit
production protocol. It was the smallest way to drive deliberate input through the real
runner stream without modifying production files. The production implementation should
make `curie_progress` a platform-owned built-in tool with a versioned worker-local input
model, or use a scoped out-of-band progress ingress if the ADR rejects a special
platform-tool interpretation of the existing `ToolNote` event. Neither option requires an
ACI schema change.

## Preconditions and scope

- `.projects/sre-bot-alert-to-pr-roadmap.md` existed.
- No `.projects/spikes/sre-progress-contract.md` existed before this run.
- The spike used current `origin/next`, where the roadmap says the adjacent reply and SRE
  capabilities live.
- PR #1773 and issues #1520/#1526 were read as adjacent behavior and constraints, not
  counted as proof.
- The disposable harness used the real worker `Kernel`, real `RunnerClient` over HTTP, real
  `SandboxSubstrate`, real thread locking/markers/completion outbox, and a disposable real
  Valkey. The existing kernel harness faked the Kubernetes client, Slack sink, and model
  frames; no live Slack, GitHub, production, staging, or deployment was touched.

## What the executable harness proved

| Requirement | Result | Observation |
| --- | --- | --- |
| Stable progress IDs | Pass | Every state/outbox key used `turn-42:investigation`; deliveries derived stable IDs from it. |
| Idempotent state update | Pass | The identical `u-investigating` input arrived twice and produced one visible state update. The final stored revision was `u-testing`. |
| Bounded durable milestones | Pass with reply-wire addition | A Valkey reservation capped the set at three. A fourth distinct milestone was refused. |
| Crash-safe milestone retry | Pass with reply-wire addition | The `evidence` milestone was sent, the process was failed before outbox acknowledgement, and recovery retried the same delivery ID. Two delivery attempts produced one visible post and zero pending records. |
| Current generic post negative | Fails today, as expected | The same two-attempt crash shape with unmodified `ReplyPost` produced two visible posts because the event has no delivery ID. |
| Approval reuse | Pass | One UUID-backed `ConfirmIntent` produced one approval post; the existing Slack adapter test confirmed that UUID becomes `client_msg_id`. |
| Steering prompted by progress | Pass | After the progress and evidence milestone were visible, `focus on deploy logs` reached the live runner through the real same-thread steer path; the original turn remained the only opened model turn. |
| One canonical final reply | Pass for the shipped placeholder steer shape | With no-edit answer streaming, the original turn posted exactly one final answer and cleared its completion outbox. The separate steer entry also emitted its own `turn.completed` bookkeeping event; that event is not another answer. |
| Frozen packages untouched | Pass | `git diff --exit-code -- packages/aci-protocol packages/plugin-format` was clean. |

### Important rendering edge found

With both the original turn and its follow-up placeholderless, today's worker posts a
bot-authored `Folded into the in-progress reply above.` receipt and later posts the final
answer. That is two bot text posts, even though only one is the final answer. With today's
normal addressable steer placeholder, the receipt edits that placeholder and the original
turn still creates one final reply.

The production progress work must therefore coordinate with #1526: when initial and steer
placeholders are removed, a successful steer should be acknowledged silently or by
updating the progress card, not by creating a new receipt message. Otherwise the progress
contract can satisfy “one final answer” technically while violating the intended
post-once Slack experience visibly.

## Exact commands and observed output

Disposable Valkey:

```bash
docker run -d --name curie-sre-progress-spike-20260826 \
  -p 127.0.0.1::6379 valkey/valkey:8-alpine \
  valkey-server --requirepass valkeypass
docker port curie-sre-progress-spike-20260826 6379/tcp
```

Observed:

```text
127.0.0.1:32920
```

Executable mechanism, negative control, and steer-rendering control:

```bash
TEST_VALKEY_PORT=32920 TEST_VALKEY_PW=valkeypass \
  uv run pytest -q -s \
  apps/worker/tests/kernel/test_sre_progress_contract_spike.py
```

Observed:

```text
NEGATIVE_RESULT {"crash_retry_attempts": 2, "current_reply_post_has_delivery_id": false, "visible_duplicate_posts": 2}
.STEER_SHAPE_RESULT {"final_reply_posts": 1, "folded_receipt_posts": 1, "placeholderless_steer_bot_posts": 2}
.SPIKE_RESULT {"approval_posts": 1, "crash_retry_attempts_same_delivery_id": 2, "final_reply_posts": 1, "idempotent_update_attempts": 2, "milestone_cap": 3, "no_aci_path": true, "pending_outbox": 0, "progress_id": "turn-42:investigation", "required_reply_wire_gap": "delivery_id on durable progress events", "state": {"state": "testing", "text": "Verifying the narrowed hypothesis", "update_id": "u-testing"}, "steers": ["focus on deploy logs"], "turn_completion_events": 2, "visible_milestones": 3, "visible_state_updates_for_duplicate": 1}
.
3 passed in 0.09s
```

Existing approval identity and completion-outbox sibling proofs:

```bash
TEST_VALKEY_PORT=32920 TEST_VALKEY_PW=valkeypass \
  uv run pytest -q \
  apps/worker/tests/test_slack_sink.py::test_uuid_approval_post_uses_the_record_id_as_slack_idempotency_key \
  apps/worker/tests/kernel/test_completion_outbox.py::test_a_failed_emit_leaves_the_record_and_the_sweeper_delivers_it
```

Observed:

```text
..                                                                       [100%]
2 passed in 0.09s
```

Harness lint and frozen-boundary check:

```bash
uv run ruff check apps/worker/tests/kernel/test_sre_progress_contract_spike.py
git diff --exit-code -- packages/aci-protocol packages/plugin-format \
  && echo 'frozen_contract_diff=none'
```

Observed:

```text
All checks passed!
frozen_contract_diff=none
```

The harness file was disposable and was removed after these observations were copied here.

## Mechanism interpretation

The proof composes four already-built properties rather than asking ACI to become a UI
protocol:

1. The runner can identify an intentional platform progress operation while ordinary tool
   notes remain internal and never leak raw tool chatter to the user.
2. A worker-owned progress coordinator persists `progress_id`, revision, state, milestone
   reservations, and pending delivery before calling the existing neutral `ReplySink`.
3. `reply.update` targets the progress card's adapter-minted `reply_ref`; retrying the same
   content against that ref is idempotent. `reply.post` creates a milestone and therefore
   needs the stable `delivery_id` that generic posts lack today.
4. The model answer remains under `_ThrottledReply` with no-edit streaming, and the shipped
   completion outbox still owns terminal `turn.completed`. Progress never becomes answer
   text and never creates a second final answer.

`turn.status` remains useful for ephemeral liveness, but it is deliberately best-effort and
is not the durable task record. The mutable progress card is the durable human-readable
state; milestone replies are the bounded durable history.

## Slack rendering semantics that still need Brian's decision

Recommended default for the ADR: **one mutable task card plus at most three durable
milestone replies; approval cards and the canonical final answer do not consume the three
milestone slots.** This matches the roadmap recommendation while keeping a hard spam bound.

Brian still needs to pin:

1. Whether the default is the recommended card-plus-three shape, milestone-only, or
   card-only.
2. Which transitions earn a durable milestone. Recommended: intake/evidence acquired,
   material hypothesis or scope change, and verification result. Awaiting approval remains
   the existing approval card, not a duplicate milestone.
3. Whether the completed progress card remains in the thread marked `complete`/`failed`, or
   is replaced/retired after the final answer. Recommended: retain it as a compact task
   history and keep the final answer separate.
4. How a placeholderless steer is acknowledged under #1526. Recommended: update the task
   card or stay silent; do not post the current folded-receipt message.
5. Whether the cap is per turn or per logical thread across approval resumes. Recommended:
   per logical turn chain, so a resolve/resume does not reset the spam budget.

## File-level implementation plan and size

**Size: M, approximately 6–9 production files plus 5–7 test/schema artifacts, 700–1,100
lines, and 4–7 engineering days after the ADR pins semantics.** Cluster/Slack E2E evidence
is additional elapsed verification time, not a reason to expand the implementation.

1. `runner/src/curie_runner/progress_tool.py` (new) and
   `runner/src/curie_runner/session.py`: register a platform-owned `curie_progress` tool;
   validate stable ID/state/text/milestone intent locally. Do not expose arbitrary channel
   addresses, reply refs, endpoints, or credentials to the model.
2. `runner/src/curie_runner/translate.py`: surface only the validated platform progress
   operation to the worker while leaving ordinary `ToolNote` behavior unchanged. If the ADR
   rejects a special interpretation of the existing event, replace this step with a scoped
   API/worker progress ingress; do not add an opaque, undocumented string convention.
3. `apps/worker/src/curie_worker/progress.py` (new): own the state machine, stable progress
   IDs, revision/idempotency checks, milestone reservation cap, durable pending/delivered
   records, and recovery sweep. Keep this out of sacred routing logic.
4. `apps/worker/src/curie_worker/kernel.py`: one narrow handoff for validated deliberate
   progress and lifecycle cleanup; ordinary tool notes remain ignored. The coordinator
   receives the already-resolved `ReplyTarget`/`TargetRoute` and never branches on channel
   kind.
5. `packages/channel-protocol/src/channel_protocol/reply.py` plus generated schema/tests:
   add a stable `delivery_id` to durable progress `ReplyUpdate`/`ReplyPost` events. Keep the
   existing reply wire versioning rules; this package is not one of the frozen stop
   boundaries.
6. `apps/worker/src/curie_worker/slack_sink.py`: pass `delivery_id` as Slack
   `client_msg_id` for new progress/milestone posts, mirroring the existing approval UUID
   behavior. Render generic `OutboundMessage` updates as a task card without entering the
   approval-settlement renderer.
7. `apps/worker/src/curie_worker/reply_sink.py` and non-Slack adapter conformance tests:
   preserve the delivery ID on HTTP and require adapter idempotency at the neutral boundary.
8. `apps/worker/tests/kernel/`: real-Valkey regressions for duplicate state, cap overflow,
   crash-before-send, crash-after-send-before-ack, recovery sweep, steer during progress,
   approval/resume continuity, and exactly one final answer. Add a mutation-style negative
   that removing `delivery_id` recreates the duplicate proved here.
9. `apps/worker/tests/test_slack_sink.py` and local Slack E2E: assert card update versus
   milestone post semantics, `client_msg_id`, placeholderless steer behavior, and the
   Brian-approved cap/rendering choice. Drive the required local tier and a real Slack
   integration before calling production implementation complete.

## Tier classification for this spike

- **Behavior-bearing production change:** none; only an ignored report remains.
- **Prototype evidence:** local worker path required and exercised with real Valkey and the
  real kernel/runner-client path.
- **Skill, local-release, cluster, live-provider:** not applicable because the disposable
  harness changed no plugin packaging, released artifact, chart/sandbox contract, provider
  routing, credential resolution, or cost accounting.
- **External integration:** Slack was intentionally faked; this spike proves mechanism, not
  live Slack rendering. A real Slack pass is required after the rendering decision and
  production implementation exist.

## Cleanup

The temporary test harness, disposable Valkey container, and temporary Valkey data were
removed. No Curie compose stack was started. Pre-existing unrelated containers were not
touched.
