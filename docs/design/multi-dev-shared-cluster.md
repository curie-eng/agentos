# Design pass: multi-dev workflow against a shared running cluster

> Status: **Design / research pass** for epic
> [#44](https://github.com/curie-eng/curie/issues/44). Resolves the epic's
> deferred open questions on **isolation, targeting, and context**, and drafts the
> concrete follow-up issues the design implies. No implementation is committed in
> this doc.
>
> Related: [#40](https://github.com/curie-eng/curie/issues/40) (target-noun CLI
> surface, the `curie context use ...` future-work note lives here),
> [ADR-0008](../adr/0008-multi-tenancy.md) (multi-tenancy: pooled RLS +
> hard-siloed compute), [ADR-0023](../adr/0023-controller-networkpolicy-rbac-cluster-read-namespace-mutate.md)
> (controller RBAC boundary), [ADR-0028](../adr/0028-substrate-is-resilience-fallback-not-product-swap-axis.md)
> (substrate stays core-with-fallback — multi-dev isolation is solved *within* the
> Kubernetes substrate, not by new substrates), and the
> [`cluster` runbook](../operations.md).

## Problem recap

The `cluster` verbs work for a **single operator** standing up and driving their
own release. Today each dev either runs their own local stack (`skill`/`local`
targets) or installs their own cluster release. There is no defined story for
**several developers iterating against one shared running cluster at the same
time**.

The single-operator assumptions that break at team scale, grounded in the code:

- **`curie cluster message` follows the release transport.** It opens its own
  `kubectl port-forward`s. When a dispatcher is connected, it posts a placeholder
  and routes replies to the agent's bound Slack channel. Without a dispatcher,
  it uses a per turn terminal reply stub and prints the reply.
- **Agent targeting is "the sole deployed agent."** `cluster message` resolves the
  target agent from the single deployed agent's `slack_channel`, and treats zero
  or multiple deployed agents as an error (`message.rs`). With N devs each
  deploying an agent, "which agent" is ambiguous by construction.
- **One of everything.** One namespace, one Slack app, one model credential, one
  API key, one RustFS bucket, one Langfuse project (ADR-0008 context). The compute
  sandbox is already hard-siloed (default-deny egress, gVisor, non-root,
  per-run budgets — ADR-0006); the gap is the **control plane**, exactly as
  ADR-0008 frames it.
- **NodePort exposure is singular and host-global.** `cluster up` exposes the UI
  and Langfuse on node ports; a second dev's release (or a second exposure) would
  collide on the same node port, and NodePort is awkward behind a managed control
  plane (EKS) without a LoadBalancer/ingress.

## Scope and non-goals

**In scope:** the *developer workflow* for many devs sharing one cluster —
isolation between their agents, how a command targets a specific dev's agent, and
the ambient "which cluster / which release am I talking to" context. This is a
**dev-collaboration** concern, distinct from (though it reuses the primitives of)
**ADR-0008 multi-tenancy**, which is about untrusted *customer* companies sharing
hosted infra with an always-on tenant boundary.

**Non-goals:** building the hosted multi-tenant control plane (ADR-0008 / epic
#158); introducing new substrates (ADR-0028 settles that substrate choice is not
the answer here); a full identity/SSO system for devs.

**Relationship to ADR-0008.** Multi-dev-shared-cluster is the *weaker,
cooperative* sibling of multi-tenancy. Devs are trusted teammates, not adversarial
tenants, so we do not need database-forced RLS between them — a naming/label
convention plus namespace scoping is proportionate. Where the two overlap (per-dev
scoping key, per-namespace compute), this design deliberately picks primitives
that ADR-0008's tenant work can later subsume rather than contradict: a `dev`/
`owner` scope key is a natural precursor to a `tenant_id`.

## The open questions, answered

### Q1. What is deployed once vs per-dev? What do the commands look like?

**Recommendation: one shared platform install, per-dev agent deployments.**

- **Deployed once (by a cluster admin/CI, not per dev):** the platform Helm
  release — API, worker, dispatcher, Valkey, Postgres, RustFS, Langfuse, the
  Agent Sandbox controller + warm pool, and the ingress/LoadBalancer. This is the
  "run against a running cluster you did **not** install" experience (Q4): a dev
  *connects* to it, they do not `helm install` their own.
- **Per-dev:** an **agent deployment** (an `agents` row + `agent_version` +
  `deployment`, and the runner sandboxes it claims at run time). A dev's loop is
  `curie cluster deploy` of their bundle against the shared release, then
  `curie cluster message --agent <name>` to drive it — never `cluster up`.

So the command surface shifts from "each dev installs and operates a release" to
"an admin operates one release; devs deploy and drive agents on it." `cluster up`/
`down` become admin/CI verbs; `deploy`/`message`/`status` become the per-dev
verbs, scoped to *their* agent.

### Q2. Per-dev isolation: naming, RBAC, NodePort/ingress collisions.

**Recommendation: a per-dev owner scope carried as a label + name prefix, with
per-dev runtime namespaces for compute and a single shared ingress for exposure.**

- **Naming.** Every per-dev object (agent, deployment, the runtime namespace it
  runs sandboxes in) is prefixed/labelled with an `owner` (the dev's handle, e.g.
  `arao`). `agents.name` is globally unique today (ADR-0008), so either (a) scope
  uniqueness to `(owner, name)` or (b) keep names globally unique and require the
  `owner` prefix by convention. Recommend (a) with an explicit `owner` column so
  targeting is exact, not a string-prefix guess.
- **Compute isolation.** Reuse the ADR-0008 primitive: **namespace-per-owner** for
  the runner sandboxes (`curie-run-<owner>`), so one dev's sandboxes, quotas,
  and NetworkPolicies never touch another's. The controller's
  cluster-read/namespace-mutate RBAC boundary (ADR-0023) already supports mutating
  within a runtime namespace while reading cluster-wide, so this is an extension
  of an existing seam, not a new trust model.
- **RBAC (devs, not pods).** Devs get a scoped kubeconfig/role that permits
  `deploy`/`message` against the shared release and their own runtime namespace,
  but **not** `helm upgrade` on the platform release. This prevents one dev's
  message transport changes from affecting everyone else.
- **NodePort/ingress collisions.** Stop exposing per-dev NodePorts. The shared
  release exposes **one ingress / LoadBalancer**; per-dev/per-agent surfaces are
  **path- or host-routed** (e.g. `…/agent/<owner>/<name>/` or
  `<owner>-<name>.<cluster-domain>`). This resolves both the "two devs collide on
  a node port" problem and the "NodePort is awkward on EKS" problem (Q6) in one
  move: managed clusters get a real LoadBalancer/ingress; local single-node
  (kind/k3s) keeps NodePort but for the **one** shared exposure, not per dev.

### Q3. How does `cluster message` target a *specific* deployed agent?

The remaining ambiguity is which deployed agent should receive the turn:

1. **Explicit agent targeting.** Add `--agent <name>` (and/or `--owner`) to
   `cluster message`, replacing the "sole deployed agent" resolution. Ambiguity
   (multiple agents) becomes "pass `--agent`," not an error, and targeting is
   exact equality on `(owner, name)`.

### Q4. The "run against a running cluster you did not install" experience.

**Recommendation: connect-to-existing is a first-class mode, distinct from
install.** A dev should never `helm install` to drive an agent on a shared
cluster. Introduce a **connect** step that records "which cluster + which release
+ which owner I am" (see Q5) and gates the per-dev verbs, while `cluster up`/`down`
stay admin/CI verbs that a dev's scoped role cannot run. Practically: `curie
context use <cluster>/<release>` (Q5) *is* the connect step; after it, `deploy`/
`message`/`status` operate against that release without any install.

### Q5. Does this want an ambient context selector (`curie context use ...`)?

**Recommendation: yes — an ambient context is the right substrate for multi-dev,
and it is the natural home for "which cluster / which release / which owner."**

- Introduce `curie context` (the future work already noted on #40): `context
  use <name>`, `context list`, `context show`. A context binds `{ kube-context,
  namespace, release, owner, api endpoint, api key ref }`.
- The per-dev verbs (`deploy`, `message`, `status`) read the active context
  instead of requiring `--namespace`/`--release`/`--agent`-owner on every call,
  which is what makes the multi-dev loop ergonomic rather than a wall of flags.
- This mirrors `kubectl config use-context` / `helm --kube-context` and is the
  minimal ambient state that Q1–Q4 all lean on. It is also forward-compatible with
  ADR-0008: an `owner` in the context is a precursor to a `tenant_id` at ingress.
- **Caveat:** ambient context is a footgun for destructive verbs (the `--local`
  lesson from #40 — a silent default targeting the wrong place). Mitigation: keep
  destructive/admin verbs (`cluster up`/`down`) **explicit and loud** (echo the
  resolved cluster/release, require `--yes`), and never let the
  ambient context silently escalate a dev into an admin action.

### Q6. Managed-cluster reachability (EKS) vs local single-node (kind/k3s).

**Recommendation: exposure is a property of the shared install, chosen once by the
admin, not per dev.**

- **Managed (EKS/GKE/AKS):** the one shared release exposes a **LoadBalancer +
  ingress**; per-agent surfaces are host/path-routed behind it (Q2). No per-dev
  NodePorts.
- **Local single-node (kind/k3s):** NodePort stays fine for the **single** shared
  exposure; per-agent routing is path-based behind that one node port.
- The dev's `context` records the resolved base URL either way, so `message`/
  `status` do not care which exposure backs it. This keeps the "NodePort locally,
  LoadBalancer on managed" difference an **install-time admin choice**, invisible
  to the per-dev loop.

## Summary of the recommended model

| Concern | Single-operator today | Multi-dev recommendation |
|---|---|---|
| Platform install | Each dev installs a release | **One shared release**, admin/CI-owned |
| Dev's loop | `cluster up` + deploy + message | **connect (context) → deploy → message**, no install |
| Agent targeting | "the sole deployed agent" | **`--agent`/`--owner` exact match**, from context |
| Reply routing | per turn terminal stub or bound Slack channel | **target the agent by owner and name** |
| Compute isolation | one runtime namespace | **namespace-per-owner** (reuses ADR-0008/0023) |
| Exposure | per-release NodePort | **one shared LoadBalancer/ingress**, host/path-routed |
| "Which cluster/release" | flags per call | **`curie context use`** ambient selector |
| Dev RBAC | full operator | **scoped role**: deploy/message yes, `helm upgrade` no |

## Proposed follow-up implementation issues

The design implies these concrete, independently-shippable issues (to be filed
under epic #44). Listed here as the design's decomposition; ordered roughly by
dependency.

1. **`curie context` ambient selector.** `context use/list/show` binding
   `{kube-context, namespace, release, owner, api endpoint, api-key ref}`; the
   per-dev verbs read it; destructive/admin verbs echo the resolved target and
   require `--yes`. (Foundational — Q4/Q5.)
2. **Explicit agent targeting on `cluster message`/`status`.** `--agent`/`--owner`
   exact-match resolution replacing "sole deployed agent"; multiple agents →
   "pass `--agent`", not an error. (Q3.)
3. **`owner` scope on agents + namespace-per-owner runtime isolation.** Add an
   `owner` column, scope uniqueness to `(owner, name)`, and run each owner's
   sandboxes in `curie-run-<owner>`; extend controller RBAC (ADR-0023) to the
   per-owner namespaces. (Q2 — precursor to ADR-0008 `tenant_id`.)
4. **Per-dev scoped RBAC role / kubeconfig.** A role that permits deploy/message
   against the shared release and the dev's runtime namespace but **not** `helm
   upgrade` on the platform release. (Q2.)
5. **Shared ingress / host-path per-agent routing; retire per-dev NodePorts.**
   One LoadBalancer+ingress on managed clusters (NodePort only for the single
   shared exposure on kind/k3s), with per-agent host/path routes. (Q2/Q6.)
6. **`cluster` runbook + connect-to-existing docs.** Document the admin-installs-
   once / devs-connect model, the context workflow, and the destructive-verb
   guards. (Q4 — depends on 1–3.)

## Open risks / things to validate during implementation

- **Ambient-context footgun.** Verify the destructive-verb guards actually prevent
  a dev from admin-escalating via a stale context (the #40 `--local` lesson).
- **Cooperative-not-adversarial assumption.** This design trusts teammates. If a
  shared *staging* cluster ever hosts semi-trusted users, the boundary must harden
  toward ADR-0008 (RLS, always-on tenant scoping) — the `owner` primitives here
  are chosen to make that migration additive.
