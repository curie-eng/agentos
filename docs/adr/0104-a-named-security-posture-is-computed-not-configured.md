# 104. A named security posture is computed from the layers that exist, never configured

Date: 2026-08-10

Status: Accepted

**Partially superseded when [ADR 0110](0110-deterministic-security-posture-classification.md)
is Accepted.** When Accepted, ADR 0110 supersedes only Decision section 4 of
this ADR by defining `declared`, the classification order, and the tie break
that makes a posture name unique. The rest of this ADR remains in force.

Implements [#1226](https://github.com/curie-eng/curie/issues/1226).

Deliberately does NOT depend on [#158](https://github.com/curie-eng/curie/issues/158)
(multi-tenancy) or [#515](https://github.com/curie-eng/curie/issues/515)
(monotonic policy resolution). The Decision says why.

## Context

Curie has the machinery: ADR-0010 (gates), ADR-0050 (armed exactly or not at
all), ADR-0056 (grantability is an operator opt-in), ADR-0035 (bounded one-shot
allowance), ADR-0067 (NetworkPolicy is additive), ADR-0077 (no skill-tier durable
approvals). Each is right on its own.

What is missing is the sentence an evaluator needs. "How locked down is this
before I point it at anything real" is currently assembled by hand from six ADRs
and four code locations, which is a real adoption cost on exactly the
security-conscious self-host buyer.

Three facts about the tree, measured rather than assumed, bound what can be
decided today:

- **No organization tier.** `models.py` declares no `Org`/`Tenant` and no
  `org_id`; `org_name` is a `Settings` string the console renders. The layers
  that exist are **deployment** (chart values, process env), **agent** (a row),
  and **bundle** (the manifest).
- **No content screening.** No screening, classifier, or provenance-labelling
  mechanism exists anywhere in the tree.
- **No policy resolver.** Defaults are scattered by construction: the
  self-approval block is in the API authorizer, default-deny egress is a chart
  value, gates are in the bundle manifest, model and thinking are agent columns.

## Decision

**A posture is a NAME for a computed result, not a value an operator sets.** A
resolver reads the deployment, agent and bundle layers, reports the effective
answer per axis together with the layer that imposed it, and the posture name
labels the shape of that answer.

### 1. Computed, never configured

There is no `posture:` setting. An operator changes a posture by changing a
constraint, so the name can never disagree with what is enforced.

This is the load-bearing choice. A configured posture is a second policy engine:
it must be reconciled against the settings it claims to summarize, and
reconciliation is where drift lives. A computed name cannot drift from what it is
computed from.

### 2. Three layers, tightening only

Composition is monotonic: **deployment** to **agent** to **bundle**. A narrower
layer may deny or require approval; it may never widen what a broader one denied.

The organization tier is absent rather than stubbed. When #158 lands it becomes
the outermost layer under the same rule.

One conflict is recorded rather than resolved: `grantableViaPolicy` (ADR-0056) is
a LOOSENING switch declared in the bundle. ADR-0056 is coherent on its premise
that the operator authors the manifest, which inverts under a tenant-facing
lattice where the bundle author is the tenant. That premise is load-bearing, and
whoever adds the organization tier has to decide it deliberately.

### 3. The floor holds in every posture

Five denials are unconditional. No posture, layer, or bundle can turn one off:

1. **Self-approval is refused**, server-side, under every approver set.
2. **Sandbox egress is default-deny**, and ADR-0067 keeps NetworkPolicy additive
   so a second policy cannot widen it.
3. **A declared approval policy is armed exactly or not at all** (ADR-0050): a
   partially-armed gate is a failed deploy, not a quiet gap.
4. **A policy gate is not grantable** unless a manifest opts in per gate
   (ADR-0056, default false).
5. **Skill-tier durable approvals are unavailable** (ADR-0077).

The floor is what makes the most permissive posture bounded rather than open.

### 4. Names cover only axes that exist

Three names over approvals, egress, and grantability:

- **Strict** -- every declared gate armed, nothing grantable, egress allowlist
  empty.
- **Standard** (default) -- declared gates armed, grantability only where a
  manifest opted in, egress as configured.
- **Permissive** -- gates armed only where declared, grantability wherever opted
  in, egress as configured. Still bounded by the floor.

Screening is **not** an axis, because Curie has none; naming a rung it cannot
enforce is the documentation label this decision exists to avoid. When screening
ships it joins as a fourth axis without changing the shape.

The third name is **Permissive, not Dangerous**: the floor makes the stronger
word untrue, and a name that overstates how far a posture goes is worse than a
duller accurate one.

### 5. Reporting only

The resolver READS. It adds no denial, changes no enforcement decision, and sits
on no request path. Everything it reports is already enforced by the code behind
the floor above; what is missing today is a place to see it in one answer. If it
later needs to enforce, that is a different decision and a different ADR.

## Alternatives rejected

**Document the postures in `SECURITY.md` and stop.** Cheapest, and what the issue
body proposes. Rejected: a documented posture is unverifiable, nothing fails when
the prose and the defaults disagree, and the first divergence is silent.

**Make posture a setting an operator writes.** Rejected: a second source of truth
needing reconciliation with the constraints it summarizes. A posture whose name
can be wrong is worse than no posture.

**Wait for #515 and present its resolver.** Architecturally cleanest, rejected on
sequencing: #515 is unassigned and larger in scope. The resolver here is small and
scoped to three existing layers; if #515 later generalizes it, this is the
consumer that proves the shape.

**Wait for #158 and include the organization tier.** Rejected: the core security
value does not need multi-tenancy, and the lattice admits an outer layer later.

**Adopt Strict / Auto / Dangerous verbatim from the external model.** Rejected
twice: "Auto" is defined by screening Curie lacks, and "Dangerous" promises an off
switch the floor denies. The two properties worth borrowing are the unconditional
floor and the tighten-only lattice, not the names.

## Consequences

- An evaluator gets one answer to "how locked down is this", with every part
  traceable to the layer that imposed it.
- The name cannot lie: drift between documented and enforced is not expressible.
- The floor becomes a published promise. Weakening any of the five is now a
  visible ADR-level change rather than a quiet default flip.
- Screening's absence becomes visible rather than implied, which matters once
  ADR-0100's channel-read verbs give untrusted content a path into context.
- The organization tier is a known gap with a known shape.
- This authorizes a READING surface only.
