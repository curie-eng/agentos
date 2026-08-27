# SRE long-turn ownership and execution-budget spike

- **Status:** evidence complete; design accepted by ADR-0130; implementation authorized separately
- **Candidate:** `0f20d32cf523293ad72d4cb8b3486709251af860` (`origin/next`)
- **Experiment:** 2026-08-27 09:42:18–09:54:18 UTC, 720.023 seconds
- **Lane:** D, long-turn reliability
- **Implementation:** [#1971](https://github.com/curie-eng/curie/issues/1971)

## Decision summary

Curie cannot safely obtain a thirty-minute turn by changing `600` to `1800`.
The current runner timeout is a total HTTP request deadline: a healthy stream
that emitted progress at 600.210 seconds was cancelled and classified
`runner-error` at 600.595 seconds. Separately, Valkey considers a delivery
reclaimable after 900 seconds of PEL idleness. Progress, steering, and a live
handler do not refresh that clock across replicas. At the boundary, a second
consumer concurrently entered the handler while the first handler was still
alive.

The smallest safe model is an **overall delivery deadline plus a renewable,
fenced delivery lease**:

1. A configurable 1,800-second budget bounds the whole delivery, including
   retries, rather than granting 1,800 seconds to each of three attempts.
2. The consumer owns a short Valkey lease while it owns the PEL row and renews
   both together. Progress is not the heartbeat.
3. Reclaim transfers an expired lease atomically. A stale owner that loses its
   fence interrupts its runner and cannot ACK, mark done, or emit a terminal
   result.
4. The existing side-effect marker remains a hard no-replay fence.
5. The completion outbox remains at-least-once transport. Exactly-once means one
   user-visible terminal effect, enforced by the lease fence plus receiver
   deduplication on `event_id`; it does not mean an impossible exactly-once
   network send.

This does not implement the fix, alter a frozen contract, or duplicate the
narrower #1532 recovery work. The rollout fix later merged and its
terminating-worker avoidance and dead-consumer discovery are inputs to the
eventual implementation. The reply-lifecycle task's recorded ownership-policy
blocker is resolved by ADR-0130.

## Precondition and scope

The precondition passed before the timed run:

- `.projects/sre-bot-alert-to-pr-roadmap.md` existed.
- No `.projects/spikes/sre-long-turn-lease.md` existed.
- The fixed local stack ports were free, the baseline and spike locks were
  available, and no Compose project owned those ports.
- Existing unrelated observability, registry, and disposable-cluster
  containers were left untouched.

This spike used an isolated local Valkey and an in-process ACI runner. It drove
the real `RunnerClient`, worker kernel, consumer-group reclaim code, markers,
completion outbox, and Valkey PEL. Slack and the model provider were replaced by
recording/offline test doubles, matching the kernel integration-test boundary.
It did not use staging, production, a shared namespace, or a real provider.

## Exact environment

| Item | Value |
| --- | --- |
| Source | clean worktree `task/sre-long-turn-lease-spike` from `origin/next` |
| Candidate commit | `0f20d32cf523293ad72d4cb8b3486709251af860` |
| Candidate subject | `Merge pull request #1838 from curie-eng/task/866-observability-query-verbs` |
| Host | Linux 6.8.0-138-generic, x86_64 |
| Docker server | 29.1.3 |
| Python / uv | Python 3.14.3 / uv 0.10.7 |
| Queue | real Valkey from `compose.dev.yaml`, unique Compose project and volume |
| Runner | in-process ACI HTTP producer, real `RunnerClient` with 600-second total timeout |
| Worker | real kernel, `max_attempts=1` only to isolate one timeout; shipped default remains 3 |
| Reclaim | real `Consumer` / `XAUTOCLAIM`, default 900,000 ms threshold |
| Reply | recording neutral sink; zero edit throttling to timestamp every frame |
| Provider | offline local producer; no provider credential or provider request |

The exact timed command was:

```bash
CANDIDATE_COMMIT=0f20d32cf523293ad72d4cb8b3486709251af860 \
EVIDENCE_LOG=/tmp/curie-sre-long-turn-lease-20260827.jsonl \
uv run python .projects/spikes/_sre_long_turn_experiment.py \
  --timeout 600 --cadence 120 --steer-at 180 \
  --observation 720 --sample-interval 60
```

The PEL boundary used the real 900,000 ms threshold but injected age with
`XCLAIM IDLE 899000 RETRYCOUNT 1`; this avoided spending another fifteen
minutes waiting for wall-clock age. The negative ran immediately at 899,000 ms,
then the positive ran after 1.2 seconds. Age injection is the only accelerated
part of the accepted run.

## Observed behavior

### Timing, progress, and steering

| Observation | Result |
| --- | --- |
| Progress writes | 0.013, 120.024, 240.070, 360.125, 480.177, and 600.210 seconds |
| Progress delivery | All six accumulated text updates reached the neutral reply sink |
| Maximum scheduled gap | 120.057 seconds |
| Steering | Accepted at 180.068 seconds while the original turn stayed active |
| Steering settlement | One `turn.completed` for the folded steer |
| Runner cutoff | Stream cancelled at 600.594 seconds |
| Worker outcome | `runner-error`; one-attempt harness escalated for human review |
| Main terminal count | One at 600.595 seconds and still one at 720.022 seconds |
| Total observation | 720.023 seconds |

The frame at 600.210 seconds is the falsifiable negative for an idle-timeout
interpretation: recent progress did not extend the total deadline.

### Delivery ownership and reclaim

Two PEL entries were exercised against the same consumer group.

**Dead owner / rollout-loss shape:** consumer A owned a PEL row but no handler,
modeling a process terminated after group delivery. At 899,000 ms, consumer B
reclaimed zero rows. Immediately after the 900,000 ms threshold, B reclaimed
one row; the PEL changed from owner A / delivery count 1 to owner B / delivery
count 2. B processed and ACKed it, leaving zero pending rows.

**Healthy owner falsification:** consumer A entered a blocking handler and was
still live. The same before-threshold negative reclaimed zero. Immediately after
the threshold, consumer B reclaimed the row and entered a second handler while A
was still inside the first. The PEL transferred to B and delivery count became
2. Both handlers ran once. This is direct evidence that `_inflight_ids` protects
only one process and current progress or handler liveness does not protect
ownership across replicas.

### Exactly-once terminal effect

The terminal path used a separate event whose first `turn.completed` sink call
was forced to fail:

1. The runner executed once.
2. While the sink failed, delivered terminal count stayed zero and the durable
   completion outbox record remained.
3. Redelivery skipped runner execution because the event was terminal, emitted
   the stored completion, and cleared the outbox. Count became one.
4. A third delivery left the count at one.

This proves the current done-marker/outbox path prevents a second execution and
produced one terminal effect in this acknowledged-sink experiment. It does not
prove exactly-once transport across an adapter response-loss window; the design
below preserves at-least-once delivery and requires receiver idempotency.

### Resources, containers, and provider errors

Twelve resource samples were recorded.

| Signal | Observed |
| --- | --- |
| Harness RSS | 168,718,336–168,898,560 bytes (180,224-byte spread) |
| Harness sampled CPU | 0.003%–0.296% |
| Isolated Valkey | approximately 3.3–4.0 MiB; sampled CPU approximately 0.15%–1.89% |
| Host available memory | 17,100,914,688–17,568,452,608 bytes |
| Host swap used | 714,211,328 bytes initially; 700,317,696 bytes finally; no growth |
| Containers | one spike-owned Valkey; no runner container; unrelated containers untouched |
| Provider errors | zero; provider path deliberately not exercised |

No resource pressure correlated with the timeout or reclaim behavior. The
provider result is an explicit limitation, not evidence about Anthropic or any
other provider's thirty-minute behavior.

## Current failure boundary: observed versus inferred

### Observed

- `RunnerClient` enforces a 600-second total request deadline even while NDJSON
  frames continue arriving.
- Same-thread steering works during that request.
- The 900-second PEL threshold refuses reclaim immediately before the boundary
  and transfers ownership immediately after it.
- A second replica can reclaim a healthy handler because ownership liveness is
  process-local.
- Current completion recovery can produce one acknowledged terminal effect
  across a sink failure and repeated stream delivery.

### Inferred from current code, not claimed as timed observation

- `WorkerConfig.max_attempts` defaults to 3. Each call may consume the
  600-second runner timeout, so one delivery can run for roughly 1,800 seconds
  plus backoff.
- The default 900-second reclaim threshold therefore lands during the second
  attempt of a default flag-clean retry sequence. A second replica can increase
  delivery count and concurrently process that still-live delivery.
- Raising the request timeout alone to 1,800 seconds makes the cross-replica
  overlap larger; raising reclaim above the maximum duration makes crash
  recovery unacceptably slow. These knobs cannot safely be coupled by arithmetic
  alone.
- `turn.completed` delivery is at-least-once. A receiver that applies an event
  and loses its acknowledgement may see the same `event_id` again.

## Recommended lease and deadline algorithm

### Semantics to pin

- **Budget:** one deadline for a delivery, including claim, runner calls, retry
  backoff, reclaim, and terminal cleanup. Default remains 600 seconds for
  compatibility; the supported configured value is 1,800 seconds. The initial
  owner persists the deadline from Valkey server time; each process enforces its
  remaining slice with a locally anchored monotonic clock. Attempts and reclaim
  do not multiply or reset the budget.
- **Lease:** authority to process and settle one stream entry. It is distinct
  from the logical thread lock, sandbox route TTL, runner liveness, and
  user-facing progress.
- **Heartbeat:** an operational renewal by the worker. Text deltas, tool notes,
  status captions, and Slack milestones never renew ownership.
- **Exactly once:** one terminal effect per `event_id`. Network delivery remains
  retryable and therefore at-least-once.

Recommended initial values:

| Knob | Value | Rule |
| --- | --- | --- |
| `executionBudgetSeconds` | 600 default; 1800 supported | whole delivery, not per attempt |
| `deliveryHeartbeatSeconds` | 10 | dedicated task, independent of model output |
| `deliveryLeaseSeconds` | 45 | at least 3 heartbeat periods |
| `reclaimIntervalSeconds` | 10 | derived/validated below lease duration |
| `shutdownReserveSeconds` | 60 | interrupt, terminal fencing, and connection close |
| `terminationGracePeriodSeconds` | at least budget + reserve | 1860 for a 1800-second budget |

`deliveryLeaseSeconds` is the source of truth for reclaim eligibility. Do not
retain a separately tunable 900-second threshold that can drift from the lease.

### State

For each `(stream, group, entry_id)`:

- PEL owner: Valkey consumer name.
- Lease key: `curie:delivery-lease:<stream>:<group>:<entry-id>`.
- Lease value: a random owner token plus a monotonically increasing fencing
  generation.
- Lease TTL: `deliveryLeaseSeconds`.
- Delivery state: the Valkey-time deadline and fencing generation, retained
  through the budget plus shutdown reserve.
- Existing event keys: done marker, side-effect marker, and completion outbox.

The fencing generation is not a public or ACI field. It is internal worker
state and therefore does not require a frozen-contract change.

### Acquire and heartbeat

After `XREADGROUP` assigns a new entry, run one Lua operation that:

1. Confirms XPENDING still names this consumer as owner.
2. Increments the entry's fencing generation.
3. Creates the lease with `NX` and its TTL.
4. Returns the token/generation or refuses without dispatch.

While the handler is live, a dedicated heartbeat task runs one Lua operation
every ten seconds that:

1. Confirms both the lease token and PEL owner still match.
2. Extends the lease TTL.
3. Uses same-owner `XCLAIM ... IDLE 0 JUSTID` to refresh PEL idle without
   increasing delivery count.

The real-Valkey regression must prove `JUSTID` heartbeat does not increment
`times_delivered`. If renewal returns false, the owner is fenced: set a local
lease-lost event, interrupt the runner through the existing bounded interrupt
RPC, and prohibit all settlement.

### Reclaim

On each maintenance pass:

1. Scan PEL entries whose idle time is at least the lease duration.
2. Skip entries with a matching unexpired lease before delivery-cap evaluation.
3. Dead-letter an over-cap entry only when no live lease protects it.
4. Use one Valkey-scripted operation to transfer an expired/unleased PEL entry,
   increment its fencing generation, and publish the new lease. If transfer
   loses a race, do not dispatch.
5. Check the side-effect marker before any retry. If present, settle to human
   escalation; never execute the runner again.
6. Probe the retained runner. If it still reports an active turn, interrupt and
   wait for idle/gone before retrying. An unreadable runner fails closed and is
   not run beside another possible turn.
7. Rehydrate and retry only when the old owner is fenced, the old runner is no
   longer active, and no side-effect marker exists.

This does not pretend a distributed process can be proven physically dead.
Instead, it makes its **authority** demonstrably dead: the PEL transfer and
fencing generation are serialized in Valkey, and stale authority cannot settle
the event.

### Terminal settlement

Replace the separate ownership-blind terminal writes with one
`complete_if_owned` Lua operation:

1. Verify the caller's current lease token/generation.
2. If an existing done marker or done completion already proves terminal state,
   return `already_terminal`.
3. Write the completion outbox record and done flag/marker atomically.
4. Return `won`; only that caller may attempt terminal delivery.

A lease loser returns without ACK or terminal output. The current outbox then
retries `turn.completed` by stable `event_id`. An adapter may claim exactly-once
terminal effect only when its receiving boundary applies that identifier
idempotently or it mutates one stable target. A sender-side receipt flag cannot
close the apply-success/ack-loss window. ACK and lease deletion happen only
after the kernel returns a settled result.

### Shutdown and rollout

On SIGTERM a worker stops `XREADGROUP` immediately but continues heartbeat and
in-flight processing until the delivery deadline plus shutdown reserve. A
voluntary rollout therefore drains rather than causes takeover. If the process
or pod is force-killed, renewal stops; a replacement becomes eligible after the
short lease, follows the side-effect and runner-liveness gates, and resumes or
escalates safely.

The chart must reject a termination grace shorter than the configured execution
budget plus reserve. The current 1,800-second grace happens to equal the desired
budget and leaves no cleanup reserve.

## File-level implementation scope

The implementation should stay in these boundaries:

| File or area | Change |
| --- | --- |
| `apps/worker/src/curie_worker/delivery_lease.py` | New Valkey Lua-backed acquire, renew, transfer, release, and fence helper |
| `apps/worker/src/curie_worker/stream_consumer.py` | Shared lease lifecycle around dispatch/reclaim; cover runs and eval sibling lanes by construction |
| `apps/worker/src/curie_worker/consumer.py` | Pass the lease/fence into the kernel; make cap/reclaim lease-aware; drain on shutdown |
| `apps/worker/src/curie_worker/kernel.py` | One overall deadline, lease-loss cancellation, side-effect gate, and fenced terminal settlement; sacred module, one owner and adversarial review |
| `apps/worker/src/curie_worker/markers.py` | Atomic `complete_if_owned`; preserve generation-checked outbox clearing |
| `apps/worker/src/curie_worker/runner_client.py` | Per-turn remaining-time timeout without replacing the worker-wide client; keep interrupt's five-second control timeout |
| `apps/worker/src/curie_worker/config.py` | Typed budget/heartbeat/lease/reclaim/reserve settings and relational validation |
| `apps/worker/src/curie_worker/run.py` | Wire settings and coordinated shutdown |
| `apps/worker/tests/kernel/` | Real-Valkey lease, cross-replica, side-effect, terminal-outbox, steer, and shutdown regressions |
| `apps/worker/tests/eval/` | Prove shared lease behavior does not drift on the eval consumer lane |
| `compose.dev.yaml` and generated release Compose | Expose the same worker settings for local and released loops |
| `charts/curie/values.yaml`, `values.schema.json`, `templates/worker.yaml` | Values-based settings, env wiring, and grace validation/rendering |
| `charts/curie/ci/worker-ttl-bounds-assertions.sh` | Positive and rejecting render assertions, including integer serialization |
| Telemetry | Lease renew/loss, stale-owner fence, reclaim reason, budget remaining, and terminal-dedupe metrics |

No change is required in `packages/aci-protocol` or
`packages/plugin-format`. If implementation discovers otherwise, stop at the
frozen-contract boundary.

## Decisions resolved after the experiment

[ADR-0130](../../docs/adr/0130-a-delivery-has-one-deadline-and-one-renewable-fenced-owner.md)
records the maintainer-approved boundary:

1. The first implementation uses an operator-wide worker budget. Per-agent or
   per-turn persistence is separate API/database/UI work.
2. The 10/45/10 heartbeat, lease, and reclaim defaults are accepted, with a
   60-second shutdown reserve and fail-closed behavior during Valkey outages.
3. Exactly-once means one user-visible terminal effect at an idempotent receiving
   boundary. Network delivery remains at-least-once.
4. The deadline is persisted from Valkey server time so reclaim cannot reset it;
   an owner uses monotonic elapsed time within its process.
5. The merged #1532 rollout fix supplies terminating-worker avoidance and
   dead-consumer candidate discovery. A live lease remains the authority that
   prevents premature reclaim.
6. Human progress identifiers and rendering remain separate. Delivery heartbeat
   is operational and invisible to the user.

## Acceptance matrix for the future implementation

| Criterion | Required positive proof | Falsifiable negative / independent path | Tier |
| --- | --- | --- | --- |
| 30-minute budget | A 1,800-second turn completes under one delivery deadline | Three configured attempts cannot consume 5,400 seconds; total stops at the one deadline | local + cluster |
| Healthy ownership | Two replicas run while one turn exceeds the old 900-second boundary; one handler only | Disable heartbeat or kill owner; replacement takes ownership after lease expiry | local + cluster |
| PEL accounting | Heartbeats refresh idle while delivery count remains 1 | Remove `JUSTID`; regression detects count growth and fails | local, real Valkey |
| Dead-owner recovery | Force-kill owning worker; replacement fences, checks runner, then resumes/retries | Keep old lease renewing; replacement must not enter handler | cluster |
| Rollout | SIGTERM stops new reads and drains the live turn through completion | Grace below budget + reserve is rejected before deployment | cluster/chart runtime |
| Side effects | Reclaim with no side-effect marker may retry after old runner is idle | Marker present causes human escalation and zero second runner execution | local + cluster |
| Terminal effect | Fail first terminal send; recovery produces one visible terminal state | Third delivery and stale owner cannot add another terminal effect | local + real adapter |
| Progress | Existing frames continue at a bounded cadence throughout the long turn | Silence does not expire the lease; progress is not ownership | local + external Slack composition |
| Steering | Same-thread steer is accepted before and after the old 600-second point | Steer after completion becomes a new turn, not a mutation of the closed delivery | local + external Slack composition |
| Provider | Real configured provider completes or reaches a classified provider error inside the overall budget | Invalid/removed credential fails visibly with no ownership leak or duplicate terminal | live provider |
| Resources | CPU, RSS, Valkey memory, PEL size, and container count remain bounded for 30 minutes | Kill/restart sampling shows no orphan runner or unbounded pending growth | cluster |
| Frozen contracts | Contract diff is empty | Contract check fails any accidental ACI/plugin-format change | CI |

For the future behavior-bearing change: `local` and `cluster` are required;
`live provider` and the real external Slack composition are required before the
Phase 4 exit gate is claimed. `skill` is not applicable unless the diff reaches
the runner turn loop or plugin packaging. `local-release` becomes required if
released Compose or image identity changes. This report itself changes no
runtime behavior and is tier-exempt.

## Cleanup proof

The accepted run's process exited zero. Its trap removed the exact
spike-owned Valkey container, volume, and network. Subsequent inspection found
no `curie-sre-long-turn-lease` or `curie-runner` container. The fixed dev-stack
ports were not left listening, both locks were released, and unrelated
containers remained running.

The earlier interrupted control run left only the same named disposable test
container, volume, and network; those exact three resources were explicitly
removed before the accepted rerun. No shared or unrelated resource was removed.
