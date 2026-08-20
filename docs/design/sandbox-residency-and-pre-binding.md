# Design pass: sandbox residency and pre-binding

> Status: **Design / measurement pass** behind
> [ADR-0114](../adr/0114-session-identity-arrives-over-the-aci-so-a-sandbox-can-be-pre-bound.md)
> (Draft). This document carries the measurement method, the seams each decision
> touches, and a staged plan. **No implementation is committed here, and a Draft
> ADR does not authorize one** ([ADR-0085](../adr/0085-acceptance-not-implementation-authorizes-an-adr.md),
> as amended by [ADR-0102](../adr/0102-accepted-alongside-implementation-with-explicit-approval.md)).
>
> Related: [ADR-0003](../adr/0003-stateless-first-rehydrate-on-resume.md)
> (stateless-first resume), [ADR-0005](../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md)
> and [ADR-0036](../adr/0036-aci-semver-and-reader-policy.md) (the ACI and how it
> may change), [ADR-0013](../adr/0013-concurrency-and-delivery-model.md) (the
> kernel invariants a bind must not break),
> [ADR-0059](../adr/0059-sandbox-is-a-bounded-resource-envelope.md) (the resource
> envelope this extends).

## Problem recap in one paragraph

A turn costs almost nothing to serve -- 2.3% to 8.8% of one core, measured
against a real model -- but a *new conversation* costs 17.39 seconds of cold
sandbox create on its critical path, guarded by a hard 90-second deadline, on
the smallest CPU share in the cluster (50m, against ClickHouse's 200m). And a
*finished* conversation keeps holding ~334 MiB of marginal memory for up to 59
idle minutes. The first fact produced two recorded production incidents; the
second is what makes a small node fill up until the first one fires. Both trace
to one mechanism: **a sandbox's capability is decided by pod environment
injected per claim**, so a pre-warmed pod cannot be useful and the pool that
would absorb the boot is architecturally unreachable.

## What was measured, and how to re-measure it

Every number in ADR-0114 came from the commands below. They are recorded here so
a reviewer can reproduce them rather than trust them. Nothing here is a fixture:
the cluster figures are from a real `curie-0.7.0` release on minikube
(12 CPU / 7.75 GiB), the process figures from cgroup v2 accounting inside a live
runner.

### Cold create, timed by phase

Apply a `SandboxClaim` directly, so the measurement isolates the substrate from
the worker. The bundle ref comes from a real `curie cluster deploy`; the plugin
dir must be injected too, or the runner boots against the template's generic
`CURIE_PLUGIN_DIR=/unused` and fails.

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  name: probe-cold
  namespace: curie
  labels:
    curietech.ai/managed-by: curie-sandbox-substrate
spec:
  warmPoolRef:
    name: curie-runner-pool
  env:
    - name: CURIE_BUNDLE_REF
      value: "bundles/<agent-id>/<version-id>.tar.gz"
    - name: CURIE_PLUGIN_DIR
      value: "/bundles/current"
    - containerName: bundle-fetch
      name: CURIE_BUNDLE_REF
      value: "bundles/<agent-id>/<version-id>.tar.gz"
    - containerName: bundle-extract
      name: CURIE_BUNDLE_REF
      value: "bundles/<agent-id>/<version-id>.tar.gz"
```

Poll `sandboxclaim` status and the resulting pod's `initContainerStatuses` and
`containerStatuses`, recording first transition to each state. Cross-check
against `kubectl get events --field-selector involvedObject.name=<pod>`, which
timestamps each init container independently -- that is what confirmed
`bundle-fetch` at ~5 wall seconds for a 6,961-byte object.

### Turn and residency cost, by cgroup accounting

`docker stats` reports a memory figure with inactive file cache subtracted,
which understated the runner by roughly 5x against a real model. Read the cgroup
directly instead:

```bash
docker exec curie-runner-local sh -c \
  'grep usage_usec /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; \
   grep -E "^(anon|file) " /sys/fs/cgroup/memory.stat'
```

Diff `usage_usec` across a turn for exact CPU-microseconds, and split
`memory.current` into `anon` (private, paid per sandbox) and `file` (page cache,
paid once per node). Enumerate the processes through `/proc` -- the image ships no
`ps`:

```bash
docker exec curie-runner-local sh -c \
  'for p in /proc/[0-9]*; do [ -r $p/status ] || continue; \
     r=$(awk "/^VmRSS/{print \$2}" $p/status); \
     c=$(tr "\0" " " < $p/cmdline | cut -c1-70); \
     [ -n "$r" ] && echo "$r $c"; done | sort -rn | head'
```

This is what showed the SDK-bundled Claude Code child at 259.5 MiB against the
Python runner's 74.1 MiB, and therefore what killed the many-sessions-per-process
alternative before it reached the ADR.

### Real-model turns without a paid credential

The runner reaches its model through `ANTHROPIC_BASE_URL`, and Ollama 0.32
implements the Anthropic `/v1/messages` surface, so a local model measures a real
streaming turn at no API cost. `--local-model` runs Curie's own pinned Ollama
image (~8.9 GB per ADR-0093's no-implicit-download rule); pointing at a host
Ollama avoids that entirely:

```bash
export ANTHROPIC_BASE_URL=http://host.docker.internal:11434
export CURIE_CREDENTIALS=ollama-local-no-auth
curie skill up --model qwen2.5:0.5b --secret ANTHROPIC_BASE_URL
```

The fake model is unsuitable for this measurement and actively misleading: it
returns canned frames in 0.10s, reports synthetic token counts, and left the
runner at 110 MiB where a real model sits near 505 MiB. Any residency or turn
figure taken under `--fake-model` should be discarded.

## The four workstreams

Decision 2 of ADR-0114 is the only one that is load-bearing on its own; the other
three are independently shippable and each stands up without the ADR being
accepted. They are ordered by risk, not by value.

### W1 -- Node-local immutable bundle cache (decision 4)

**Reclaims 4.5s of 17.4s and removes two containers from the boot path.**

The bundle is content-addressed; the deploy already prints its digest. Two
shapes, and the choice is a real one:

- A DaemonSet-populated, digest-keyed, read-only host path the sandbox mounts.
  Cheap to build, but introduces a cross-tenant shared surface beneath
  ADR-0008's per-tenant compute boundary -- which ADR-0114 explicitly declines to
  decide, so this shape needs its own ADR.
- A thin OCI layer per version, built at deploy time, letting the kubelet's
  existing image cache do the deduplication. No new trust surface, and it reuses
  machinery that already exists, at the cost of an image build inside the deploy
  path -- which matters because `git push` is the deploy
  ([ADR-0014](../adr/0014-git-push-is-the-deploy.md)).

**Recommendation: the OCI layer.** It buys the same win without asking for a new
sharing decision, and the runner base image is fixed so only a small layer
changes per version.

Seams touched: `charts/curie/templates/agent-sandbox.yaml` (init containers),
`charts/curie/values.yaml` (`bundleFetch`), the API's bundle pipeline.

### W2 -- Right-sized requests and a turn-plane priority class (decision 6)

**Removes the 4:1 starvation amplifier. No new interfaces.**

CPU requests on the runner, `bundle-fetch`, and `bundle-extract` are 50m each
against a measured 0.43m idle and ~90m active. Memory request is 192Mi against a
measured ~505 MiB. Both should come from measurement, with ADR-0059 decision 6's
operator override preserved. The `PriorityClass` extends ADR-0059 decision 5 to a
second axis: turn plane over insight plane.

This is the cheapest workstream and the one most likely to be mistaken for the
whole fix. It is not: it makes starvation less likely without removing the
wall-clock deadline that starvation attacks.

Seams touched: `charts/curie/values.yaml`, the priority class template.

### W3 -- Version-keyed warm pools (decision 3)

**Reclaims the remaining ~12s, but only after W4.**

One `SandboxWarmPool` per in-force deployment, its template carrying that
version's bundle ref, so its pods pre-fetch, pre-extract, and pre-boot. The
platform already knows the set: `curie.deployments`. Needs a bounded total pool
count, a reaper for retired versions, and zero pre-warmed pods for deployments
with no recent traffic.

**W3 does not work before W4.** A pool pod that knows its bundle still cannot be
bound while `envVarsInjectionPolicy: Overrides` forces per-claim env to replace
what the pool baked in. This ordering is the single most important thing to carry
out of this document.

Seams touched: `charts/curie/templates/agent-sandbox.yaml`,
`apps/worker/src/curie_worker/sandbox/substrate.py`, the deployment reconciler.

### W4 -- Session identity over the ACI (decision 2)

**The enabling change, and the only one that needs an ACI version.**

`CURIE_SESSION_ID`, `CURIE_HISTORY_REF`, and the runner token move out of
`SandboxClaim.spec.env` and into a request against a bound runner, joining
`/v1/event`, `/v1/steer`, `/v1/interrupt`, and `/v1/reset` on the existing
server. The substrate's `claim()` and `resume()` stop building a boot-env overlay
and start making one ACI call after the bind.

Constraints this must respect:

- **ADR-0013's kernel invariants.** One live session per thread stays the
  routing CAS; the finish race, the side-effect flag, and the no-auto-retry rule
  are untouched. A bind that silently allowed two sessions on one runner would
  break the thing `kernel.py` exists to protect.
- **ADR-0003's resume contract.** Rehydrate-from-history remains how a resumed
  thread recovers. This changes the *delivery* of the history ref, not the
  contract.
- **ADR-0036's reader policy.** A minor version with a compatibility window,
  and the skew surfaces at session start rather than at boot. Worth noting that
  ACI skew already fails late today -- an 0.2.7 CLI against an 0.4.1 runner boots
  cleanly and fails on the first message.
- **The runner token.** Minting it per claim is what makes it die with the
  claim. Delivering it over the ACI means the bind itself must be authenticated
  by something the pool pod already holds, which is a real design question this
  document does not settle.

Seams touched: `packages/aci-protocol/schema/aci-protocol.schema.json`,
`runner/src/curie_runner/server.py`, `apps/worker/src/curie_worker/sandbox/substrate.py`,
`apps/worker/src/curie_worker/binding.py`.

### Then, and only then: short residency (decision 5)

`routeTtlSeconds` drops from 3600 to something on the order of seconds. This is
a values change, and it is safe **only** once a re-bind is sub-second -- before
that it trades a compute saving for a token bill, because a resumed thread is
cache-cold and a scaffolded bundle already re-sends 20,875 input tokens per turn.

## What a demo has to show

The claim that matters is not "faster". It is that **the deadline stops being
reachable**. A convincing demonstration shows, side by side:

1. Today's claim-to-ready wall clock on a quiet node (~17s), then the same
   measurement with a competing CPU load that reproduces the incident's 4:1
   share, pushing it toward the 90-second ceiling.
2. The same two runs after W1-W4, where the second is indistinguishable from the
   first because a bind is not a boot.
3. Pod count dropping to zero while a thread stays answerable, with the next
   message answered in under a second.

The third is the one that reads as a product capability rather than an
optimisation.

## Open questions

- **How is a bind authenticated** if the runner token no longer arrives as pod
  env? This gates W4 and has no answer yet.
- **What is the per-session marginal cost inside one process**, if the SDK could
  ever multiplex sessions in one Claude Code child? The measured 259.5 MiB child
  is per-session today; whether it must be is unmeasured, and it is the only
  path to a memory win larger than 1.25x.
- **Does the node-local bundle cache need its own ADR?** ADR-0114 says it does
  not decide the shared-surface question. W1's OCI-layer recommendation is
  chosen partly to avoid needing that decision at all.
- **gVisor overhead is unmeasured.** All figures here were taken with
  `security.gvisor.mode=off`, which is not the production default. Pre-binding
  removes gVisor's cost from the boot path but not from the turn.
