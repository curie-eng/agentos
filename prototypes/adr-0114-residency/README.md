# PT-0114: does pre-binding remove the claim deadline?

Throwaway spike behind
[ADR-0114](../../docs/adr/0114-session-identity-arrives-over-the-aci-so-a-sandbox-can-be-pre-bound.md).
Not shipped code, not maintained, and it implements none of the ADR's decisions:
it only measures whether the pre-bind path is reachable at all, and what it costs
when the node is starved.

## What it does

Four timed arms against a live release, plus a controlled neighbour:

| Arm | Claim | Node | Measures |
| --- | --- | --- | --- |
| A | carries per-claim env (today's shape) | quiet | the cold create baseline |
| B | same | saturated | whether `claimTimeoutSeconds` (90s) is reachable |
| C | carries no env, against a version-keyed pool | quiet | the pre-bind path |
| D | same | saturated | whether the deadline is still reachable |

The version-keyed pool is the ADR's decision 3 built by hand: a copy of the
shipped `SandboxTemplate` with the bundle ref and `CURIE_PLUGIN_DIR` baked in
**per pool** instead of injected per claim, plus its own `SandboxWarmPool`. It is
created alongside the shipped objects and never patches them, so a live claim on
the same cluster is undisturbed.

Contention is a Deployment of busy-loop pods that request `200m` each, matching
what ClickHouse requests, against the runner's and bundle containers' `50m`. That
4:1 request ratio is the amplifier the chart names: under contention the kernel
divides CPU in proportion to requests, not limits.

## Results (2026-08-20, two full runs)

|                       | quiet node | under contention |
| --------------------- | ---------- | ---------------- |
| today (cold create)   | 4.72s      | **never ready**  |
| pre-bound (ADR-0114)  | **0.17s**  | 7.79s            |

Run 1 measured 4.66s / timed out / 0.14s / 7.86s. Arm B crossed 90s at 91.02s and
was still not ready when the harness gave up at 110s.

The cold baseline is cluster-shaped. This cluster runs the fake model with no
observability stack and a 6,961-byte bundle, so 4-5s is its floor; the same
measurement on a real-model install with Langfuse and ClickHouse resident was
17.39s. The column that carries the argument is the second one.

## Running it

Needs a cluster you own and can saturate. It refuses any kube context whose name
does not start with `curie-demo`, because the kubeconfig this was written against
also held production contexts.

```bash
kubectl config use-context curie-demo
BUNDLE_REF="bundles/<agent-id>/<version-id>.tar.gz" python3 run_demo.py
```

`BUNDLE_REF` comes from `curie cluster deploy`. Recording:

```bash
asciinema rec --command "python3 run_demo.py" --window-size 100x36 demo.cast
agg demo.cast demo.gif --speed 3 --idle-time-limit 1.5 --font-size 15
```

## What it does not establish

- The ACI change itself. Session identity still arrives as pod env here; arms C
  and D pass **no** env, which is why they bind. How a bind is authenticated once
  the runner token stops arriving as pod env is open.
- A clean cold-path wall clock under contention. Arm B is reported as "never
  ready" rather than a number, because the point is the crossing, not the tail.
- Anything about gVisor. `security.gvisor.mode=off` throughout.
