# tests/soak

The soak and chaos suite. It proves the definition-of-done under sustained load
against a STANDING curie cluster: concurrent threads plus a mid-thread batch
job, a sandbox killed mid-run, and a resume-rehydrate under load, asserting no
cross-talk between threads, no duplicate side effects, and a sandbox-affinity
(prompt-cache warmth) proxy. It runs against a real cluster sized to the
definition-of-done target and can run three consecutive times in one invocation.

The suite drives the worker's `SandboxSubstrate` plus Valkey plus `kubectl`
directly, mirroring `apps/worker/tests/sandbox/test_e2e_k8scratch.py`. There is
no REST thread or message API to drive; a turn is a `kubectl port-forward` to the
sandbox pod followed by `POST /v1/event` (NDJSON frames ending in a `final`).

## Opt-in gate

The scenario never runs in default (PR) CI. It is gated on `CURIE_SOAK=1`, and
`curie dev soak` is the one entry point that runs it (the repo's one-entry-point
rule, `CLAUDE.md`) — the verb sets `CURIE_SOAK=1` itself and preflights the
cluster before invoking the suite.

It does run nightly: the graded parity ladder's `ladder-cluster` job
(`.github/workflows/nightly-graded-ladder.yaml`) drives `curie dev soak` against
the standing kind cluster it already installed, as a **report-only** rung. The
rung is a single step that captures the verb's exit status, prints its JSON
result to the run log and the job summary, and then ends in an explicit
`exit 0` — that, not a blanket `continue-on-error: true`, is what makes it
**unable to fail the nightly** (the #1603 report-only precedent; see #2056). Its
uv acquisition sits outside that step as a plain, unretried toolchain fetch, on
the same footing as the job's other third-party actions; if uv never installs,
the rung's captured statuses make it report "could not run" rather than redden
the job. The rung overrides the concurrency and batch knobs downward because a
2-CPU GitHub runner cannot hold the eight ready sandboxes the defaults demand.

The pure-helper unit tests in `test_harness_unit.py` always run (offline, no
cluster) and cover the deterministic harness logic.

Only `test_harness_unit.py` is in the pytest `testpaths` (`pyproject.toml`), so
the offline helpers run in default CI while `test_soak_resilience.py` (the
cluster scenario) stays out of default collection and runs only when invoked by
explicit path or under `CURIE_SOAK=1`.

## How to run

The scenario, against a standing cluster and dev stack:

```bash
curie dev soak
curie dev soak --namespace curie-g1 --pool curie-g1-runner-pool --runs 3
```

Each flag also reads its environment variable — `--namespace` from
`CURIE_SANDBOX_E2E_NAMESPACE`, `--pool` from `CURIE_SANDBOX_E2E_POOL`, `--runs`
from `CURIE_SOAK_RUNS` — so an already-exported harness environment is honored
and the bare `curie dev soak` above is the normal invocation.

**Skip contract.** With no reachable kube context, no such namespace, or no
reachable `SandboxWarmPool`, the verb emits `status: "skipped"` with a named
reason and exits **0**. It exits non-zero only when the scenario itself fails or
could not be launched.
That is what lets the nightly report-only rung distinguish "there was nothing to
soak against" from "the soak found something", instead of reporting a false
green or a false red.

Offline unit tests (no cluster, always green):

```bash
uv run pytest tests/soak/test_harness_unit.py -q
```

The underlying implementation, if you need to drive pytest directly (three
consecutive runs shown; `curie dev soak --runs 3` is the supported form):

```bash
CURIE_SOAK=1 CURIE_SOAK_RUNS=3 uv run pytest tests/soak -q
```

Size the warm pool to at least `CURIE_SOAK_CONCURRENCY + CURIE_SOAK_BATCH`
ready replicas (the `pool_ready` fixture blocks until the pool reports that many)
so the concurrent claims and the batch burst never starve.

## Environment knobs

| Variable | Default | Meaning |
|---|---|---|
| `CURIE_SOAK` | unset | Set to `1` to enable the scenario. Unset skips it. `curie dev soak` sets it itself, so you only set it by hand when driving pytest directly. |
| `CURIE_SOAK_CONCURRENCY` | `5` | Distinct concurrent Phase-A threads. |
| `CURIE_SOAK_BATCH` | `3` | Concurrent Phase-B batch-burst threads. |
| `CURIE_SOAK_RUNS` | `1` | Consecutive runs in one invocation (set `3` for the DoD). |
| `CURIE_SANDBOX_E2E_NAMESPACE` | `curie-g1` | Cluster namespace. |
| `CURIE_SANDBOX_E2E_POOL` | `curie-g1-runner-pool` | Warm pool name. |
| `TEST_VALKEY_HOST` | `localhost` | Dev-stack Valkey host. |
| `TEST_VALKEY_PORT` | `26379` | Dev-stack Valkey port. |
| `TEST_VALKEY_PW` | `valkeypass` | Dev-stack Valkey password. |
| `KUBECONFIG` | implicit | Points at the standing cluster. |
| `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | unset | Present enables live-cred content assertions and the cache-token probe. |

Without a live credential the fake-model runner is in play: turns still stream a
`final` frame and every structural assertion (isolation, affinity, pod UIDs,
claim counts, injected history ref) holds, but reply-content assertions (marker
echo, content-level no-cross-talk) are skipped because the fake model does not
echo the prompt.

## Phase map

| Phase | What it exercises | Key assertions |
|---|---|---|
| A | Concurrent threads | Distinct threads bind distinct sandboxes (isolation); re-claim returns the same sandbox (affinity); with live creds each reply carries its own marker and no foreign marker (no content cross-talk). |
| B | Mid-thread batch job | A batch burst runs while Phase-A threads are held; all batch turns reach `final` and Phase-A pod UIDs are unchanged (undisturbed under load). |
| C | Sandbox killed mid-run | Unclean `kubectl delete pod`; re-claim yields a fresh pod (new UID) that answers `/healthz` and reaches `final`; exactly one live `SandboxClaim` survives for the thread hash (no orphan or duplicate). |
| D | Resume-rehydrate under load | Suspend deletes the pod; resume creates a new claim whose pod carries `CURIE_HISTORY_REF`; concurrently loaded threads keep their pods. |
| Cache warmth | Prompt-cache affinity proxy | A non-killed, non-suspended thread keeps the same pod UID across consecutive turns (`EVIDENCE same_pod_across_turns`). |

## Documented assumptions and gaps

1. **"Batch job" is interpreted as a concurrent burst under sustained load.** The
   batch phase launches `CURIE_SOAK_BATCH` additional threads while the Phase-A
   threads are still held claimed, and asserts the batch turns complete without
   disturbing the held threads. An alternative reading of "batch job" is an eval
   fan-out (an `XADD` to the `curie:evals` stream consumed by a separate
   consumer group). That path is not part of the sandbox substrate this suite
   drives, so it is noted here as an alternative rather than exercised.

2. **"No duplicate side effects" is asserted at the substrate level (one live
   claim survives a kill), not as end-to-end side-effect idempotency.** The
   semantic invariant, a failed run that emitted a side effect escalates to a
   human instead of auto-retrying, is the kernel's rule (the fourth rule in
   `apps/worker/CLAUDE.md`) and already has a provoking integration test in
   `apps/worker/tests/kernel`. This suite drives the `SandboxSubstrate` seam
   directly (mirroring `apps/worker/tests/sandbox/test_e2e_k8scratch.py`) and so
   asserts the observable substrate-level proxy: after an unclean kill and
   re-claim, exactly one live `SandboxClaim` remains for the thread hash (no
   orphaned or duplicated claim). Re-driving turns through the kernel path (a
   Valkey `curie:runs` producer plus a fake Slack sink plus the in-cluster
   consumer) to count actual side-effect executions is deliberately left out of
   this footprint: it would duplicate the kernel suite's existing coverage and
   pull the soak away from the substrate seam it is meant to stress. See the
   follow-up below.

3. **`cache_read_input_tokens` is not observable at the cluster level, so the
   suite asserts pod-UID affinity as the cache-warmth proxy.** The runner's OTel
   export (`runner/src/curie_runner/otel.py`, `_GenerationSpan.record_usage`)
   exports only `gen_ai.usage.input_tokens` and `output_tokens` and drops the
   cache-token fields, so Langfuse never records `cache_read_input_tokens`. It is
   asserted only at the SDK layer in `runner/tests/test_live.py`. The soak
   therefore asserts the cluster-observable property that ENABLES cache reuse:
   the same pod (same pod UID) serves consecutive turns on a thread (ADR-0003
   "same pod across turns"). A single `xfail` probe (`test_cache_read_tokens_probe`)
   attempts to read per-trace usage from Langfuse and would turn green if the
   OTel export is later extended. Follow-up: extend `_GenerationSpan.record_usage`
   to export `cache_read_input_tokens` / `cache_creation_input_tokens` so this
   probe can become a real assertion.

## Follow-ups noted (outside this footprint)

- Extend `runner/src/curie_runner/otel.py` to export the cache-token usage
  fields (see gap 3 above); the `xfail` probe becomes a real assertion then.
- Add an end-to-end side-effect-idempotency soak that drives turns through the
  kernel path (a Valkey `curie:runs` producer plus a fake Slack sink plus the
  in-cluster consumer), kills a sandbox after a side-effecting tool call fires,
  re-drives, and asserts the effect executed exactly once (see gap 2 above).
- If an eval-fanout batch interpretation is wanted, add a phase that drives the
  `curie:evals` stream and its consumer group (see gap 1 above).
