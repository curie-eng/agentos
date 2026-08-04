# CLAUDE.md - charts/curie

The umbrella Helm chart: Langfuse + Postgres + Valkey + ClickHouse + RustFS +
OTel Collector, plus the Agent Sandbox substrate and its security rails. Full
component and rail detail in `charts/curie/README.md`.

## Load-bearing invariants

- **Values, not templates, for anything that varies by environment.** Per
  the platform-level rule this repo also follows: resource limits, replica
  counts, and probe settings belong in `values.yaml` / a values overlay, not
  hardcoded in `templates/`. A value safe on a 4 GB scratch cluster can OOMKill
  on a smaller install.
- **Every backing store follows the same toggle + BYO idiom.** `<store>.deploy`
  (default `true`) gates whether the in-chart resource renders; flipping it
  to `false` repoints consumers (Langfuse env, the collector config) at the
  BYO `host`/`port`/`auth`/`existingSecret` fields on the same block. A new
  backing store must follow this exact pattern -- do not add a store with a
  different enable/disable shape.
- **Values keys are camelCase, not hyphenated.** Go templates cannot
  dot-index a hyphenated key. Keep this consistent across any new values
  additions.
- **Fail-closed egress, always.** `security.networkPolicy.allowedEgress` is
  empty by default; an unset allowlist must never mean allow-all. If you add
  a new egress destination the runner needs, it goes into this allowlist
  explicitly -- never widen the default-deny baseline itself.
- **NetworkPolicy allows are additive, never restrictive-intersecting
  (#765, ADR-0067).** A second NetworkPolicy selecting the same pods can only
  widen what Rail 1 permits, never narrow it -- there is no such thing as one
  policy overriding another. This is why the runner SandboxTemplate sets
  `spec.networkPolicyManagement: Unmanaged` whenever Rail 1 is on: it stops the
  vendored controller from reconciling its own separately-managed, broader
  egress policy for the same pods. Do not add any other NetworkPolicy-adjacent
  mechanism (another controller, an operator, a second chart) that could select
  `component: runner-sandbox` pods without checking it does not reintroduce
  this exact union-defeats-default-deny failure mode.
- **The preflights are mandatory Helm hooks, not advisory scripts.** The
  CPU-AVX/ClickHouse-pin check (`preflights.avxCheck`), the
  NetworkPolicy-enforcement probe (`preflights.networkPolicyProbe`), and the
  controller-ready gate (`preflights.controllerReady`, which fails the install
  if the vendored agent-sandbox controller cannot sync its cluster-scope
  NetworkPolicy informer -- issue #350) block a broken install. Do not make
  any of them skippable by default, and do not add a new cluster-dependent
  assumption (a CNI feature, a kernel feature, an RBAC grant the controller
  needs to start) without a matching preflight -- an assumption that silently
  fails on a customer cluster is exactly the failure mode these exist to
  prevent.
- **gVisor needs `runsc` on the node; the chart cannot install it.** On a
  cluster without it, use the ready-made overlay
  `-f charts/curie/values-e2e-nogvisor.yaml` (sets `runtimeClassName=""` and
  disables the gVisor preflight, leaves every other rail on) rather than
  hand-editing `security.gvisor.*` -- the overlay is the supported opt-out
  path for e2e/scratch clusters.
- **CRDs in `crds/` are vendored, never templated.** Helm does not
  upgrade or delete `crds/` content; a teardown needs a manual
  `kubectl delete crd <name>`. Do not move CRD definitions into `templates/`
  to make them "manageable" -- that changes install ordering guarantees
  Helm's `crds/` convention provides.
- **The controller (`agentSandbox.controller.deploy`) is cluster-scoped.**
  Install it from exactly one release per cluster; leave it `false` on any
  cluster that already runs `agent-sandbox`. Do not default this to `true`
  in a values file intended for a shared/multi-release cluster.
- **The runner image is `IfNotPresent` + prewarm, NOT `Always`.** Images pull
  from GHCR; the four Deployment-managed services default to `Always` (fresh
  `:latest` on every rollout), but the runner must not -- a sandbox pod is
  created per Slack thread, and an in-boot image download can blow the
  worker's claim timeout (live incident 2026-07-06). The runner-prewarm
  DaemonSet (`agentSandbox.runner.prewarm`) pulls the runner image at
  install/upgrade instead, and every `helm upgrade` rolls it to refresh the
  cache. Do not flip the runner to `Always` and do not disable the prewarm
  on `:latest`-tag clusters without accepting stale-image risk.

## Verify

Static / chart-authoring checks (they render manifests but NEVER run a container,
so they cannot catch a bug that only surfaces at runtime):
```bash
helm lint charts/curie
helm template charts/curie -f charts/curie/values-dev.yaml   # chart-authoring check, no cluster contact
```

Runtime check (the cheap default for a chart/sandbox/bundle change): installs a
trimmed slice, runs the bundle-fetch init pair, and exec-asserts on the runner:
```bash
curie dev chart-runtime-e2e            # implemented by scripts/chart-runtime-e2e.sh
```
A ticket whose AC is a runtime check (like #56, the bundle-fetch credential
isolation) is only satisfied by running this and pasting its output -- lint /
template do not exercise the init container or the live runner.

Cluster verification (a disposable local cluster, `kind` or `k3s`):
```bash
helm install curie-dev charts/curie -n curie-dev --create-namespace \
  -f charts/curie/values-dev.yaml
kubectl get pods -n curie-dev -w
helm test curie-dev -n curie-dev                              # re-runs both preflights + the security probe suite
kubectl logs -n curie-dev job/curie-dev-preflight-avx
kubectl logs -n curie-dev job/curie-dev-security-probe        # rails 1, 2, 4
kubectl logs -n curie-dev curie-dev-security-probe-hardening  # rail 3
```
