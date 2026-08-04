# 90. A reconciler applies connectors, so agent repos need no CLI

Date: 2026-07-31

Status: Accepted

## Context

Issue #1184. [ADR 0087](0087-the-api-renders-connector-objects-the-cli-applies-them.md)
split connector work: the API renders the Kubernetes objects, the CLI applies
them under the operator's own credentials. That kept cluster-write authority
away from the internet-facing service, which was and remains right.

It also named the cost:

> Connector hosting works only where a `kubectl`-capable caller drives the
> deploy. Git-flow pushes create a Version through the API but do not apply its
> connectors [...] Closing that requires an in-cluster reconciler with
> cluster-write authority — a decision this one deliberately defers rather than
> smuggling into the API.

That cost has now been paid in full, and it is measurable. Because every deploy
needs a `kubectl`-capable CLI near the cluster **at a version matching the
platform**, the first agent repository to adopt connectors grew a Curie version
pin, a cross-compilation workflow, an artifact staging step, and a remote
install step — roughly 185 lines in a repository whose subject is answering SRE
questions, tracked in that repository's own pinning issue.

That is the same shape as the 184 lines of hand-written Kubernetes
`connectors.yaml` replaced, reintroduced one layer up. It also produced two
production failures in a week, in opposite directions: a CLI newer than the
platform, then a platform newer than the CLI. Both were the same fact — two
components that must agree, kept in step by hand.

The scaling argument is the decisive one. One agent repository carrying a
platform version pin is a workaround. Ten repositories carrying ten pins that
drift independently is an architecture, and a bad one.

Three of the four pieces needed to remove it already exist:

- **git-flow** (`apps/api/gitflow.py`) turns a push into a Version and a
  Deployment with no CI, no CLI, and no cloud credentials in the agent repo.
- **A cluster-held GitHub credential** (`api.githubToken`, #1058) makes the pull
  model reachable.
- **Server-side target resolution** (`POST /deploy-targets/resolve`,
  [ADR 0089](0089-bundles-declare-their-deploy-targets.md)) already reads a
  bundle's `deploy.yaml` without a CLI.

Only applying the connector objects still requires a caller with `kubectl`.

## Decision

**A reconciler running in the cluster applies connector objects, converging
them to what the in-force Deployment's version declares.**

It is a separate workload, not a widening of the API's RBAC. The API keeps
`pods: list` and `pods/log: get`, and the reason is unchanged from ADR 0087:
it is the component that receives internet webhooks, and a flaw there must not
become the ability to create a pod running an attacker-named image with a
mounted credential. Adding a reconciler is how connector applying becomes
automatic **without** moving that boundary.

Rendering stays in the API. It is a pure function of the bundle, it is already
written, and duplicating it in a second component would recreate the drift
`connectors.yaml` exists to prevent. The reconciler consumes the rendered
objects; it does not re-derive them.

**The CLI path stays.** `curie cluster deploy` applying connectors directly is
how a developer works against a laptop cluster, and how anyone debugs the
reconciler itself. This adds a second path; it does not remove the first. Both
converge on the same rendered objects, so they cannot disagree about what a
connector should look like — only about when it is applied.

**Ownership stays label-based.** The reconciler prunes exactly what the CLI
prunes, keyed on `curie.dev/connector-owner`, so an object created by one and
reconciled by the other is not a special case. An object without the label was
not created by Curie and is never touched — the property that let the first
adopting repo's hand-written connector survive alongside a Curie-managed one
during migration.

## Consequences

An agent repository becomes: `plugin.json`, `SKILL.md`, `evals/cases.json`,
`connectors.yaml`, `deploy.yaml`. No version pin, no deploy workflow, no cloud
credentials, no knowledge that Curie has versions at all. Push to `dev` and the
dev agent updates; push to `main` and prod does.

**The test of this decision is that an agent repo deletes code.** If an
implementation leaves that agent repo with a `.curie-version` and a provisioning
workflow, it has not achieved the thing this ADR is for. That is a
falsifiable acceptance criterion and should be treated as one.

A new workload holds cluster-write authority. That is a real increase in blast
radius and the reason this is an ADR rather than a refactor. It is narrowed
three ways: the reconciler is not internet-facing, its RBAC covers only the
four object kinds a connector consists of, and it is namespace-scoped like the
sandbox controller's NetworkPolicy role (`agent-sandbox.yaml`) rather than
cluster-wide.

Drift becomes self-healing, which is a behaviour change with a sharp edge: an
operator's manual `kubectl edit` on a connector is reverted. That is correct
for a declared system and surprising the first time. It must be visible —
logged as a correction, not applied silently.

The reconciler needs the credential values a connector's Secret carries. Today
the CLI resolves those locally from the operator's environment or vault, which
is precisely why the cluster does not hold them. This is the hardest part of
the decision and the reason #1163 (referencing an existing Secret rather than
minting one) is a prerequisite rather than a nice-to-have: with it, the
reconciler never handles a credential value at all — it points a `secretKeyRef`
at a Secret someone else provisioned.

Until #1163 lands, a connector declaring `secrets:` still needs a CLI-driven
deploy. Partial coverage is acceptable and should be stated plainly rather than
papered over: the reconciler handles connectors whose credentials already exist
in the cluster, and the CLI remains the path for the rest.

## Alternatives considered

- **Widen the API's RBAC and let it apply.** Rejected for the reason ADR 0087
  gave and this ADR does not revisit: it is the component that receives
  webhooks from the internet, and pod-creation authority there is the outcome
  worth the most effort to avoid. Convenience is not a sufficient argument
  against a stated security boundary.
- **Keep the CLI path and make version pinning ergonomic.** Rejected: it is
  what the first adopting repo's pinning issue does, and the objection is not
  ergonomics but
  scale. A better pin is still a pin in every agent repository.
- **Have the worker apply connectors on a turn.** Rejected: the worker runs
  when someone messages the agent, so a connector would appear only on first
  use — and a deploy would report success with nothing running. Deploy-time
  and turn-time are different clocks and this belongs on the former.
- **A `curie` binary baked into the cluster that the API shells out to.**
  Rejected: it gives the API the same authority through indirection, while
  making the trust boundary harder to see rather than easier.

## Open questions for implementation

These are deliberately not decided here; they are implementation shape, and
choosing them without evidence would be guessing.

- **Reconcile loop versus deployment-triggered.** A loop converges after manual
  drift and after a missed event; a trigger is simpler, cheaper, and does not
  fight an operator. A loop is likely right, but the interval and the
  drift-correction logging matter more than the choice itself.
- **Where it runs.** A worker sidecar reuses a workload that already has
  cluster access and a database connection. A standalone controller is cleaner
  to reason about and to scope RBAC for. The sidecar is less new surface; the
  controller is easier to audit.
- **What happens when rendering fails** for the in-force version — for example
  a bundle whose `connectors.yaml` no longer validates against a newer
  plugin-format. Reverting to the previous version's objects and reporting is
  probably right; silently leaving the last-good objects in place is probably
  not.
