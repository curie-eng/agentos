# 110. Deterministic security posture classification distinguishes manifest declarations from operator gates

Date: 2026-08-16

Status: Draft

**Supersedes when Accepted: [ADR 0104](0104-a-named-security-posture-is-computed-not-configured.md),
Decision section 4.** This replaces only the posture naming rules. Its
computed, read only, monotonic, and bounded floor decisions remain unchanged.

## Context

ADR 0104 defines Strict, Standard, and Permissive by three axes: approvals,
grantability, and egress. It does not define an evaluation order or a tie break.
For a normal installation, Standard and Permissive agree on every stated axis:
ADR 0050 arms every manifest declared gate, ADR 0056 permits grantability only
where that manifest opts in, and both use configured egress.

The word `declared` also has two plausible meanings. ADR 0050 constructs the
armed tool set from the union of manifest approval policy gates and
`CURIE_APPROVAL_REQUIRED_TOOLS`. A posture resolver therefore needs to know
whether an operator supplied tool is declared, and how that source changes a
posture name.

No code reads a posture name yet. This decision fixes the record before a
resolver or reporting surface makes the ambiguity externally observable.

## Decision

**A valid installation resolves to exactly one posture by testing Strict, then
Standard, then Permissive. The first matching posture wins.** This is a
most restrictive wins rule: Strict is considered more restrictive than Standard,
which is considered more restrictive than Permissive.

### 1. Declaration has one meaning

For posture classification, a **declared gate** is a distinct tool name in the
bundle manifest's `approvalPolicy` gates. A tool named only in
`CURIE_APPROVAL_REQUIRED_TOOLS` is an **operator gate**, not a declared gate.
The runner still arms the union required by ADR 0050. This vocabulary only
distinguishes the provenance of the effective armed set for reporting.

Let `M` be the manifest declared gate names, `O` the operator gate names, `A`
the effective armed gate names, `G` the manifest declared gates opted into
grantability, and `E` the configured egress allowlist. ADR 0050 requires a
running installation to have `A` include `M` and `O`. ADR 0056 requires `G` to
be a subset of `M`.

### 2. Evaluate in this order

1. **Strict** matches when `G` is empty and `E` is empty. Additional operator
   gates do not weaken this result because they only add an approval requirement.

2. **Standard** matches when every effective armed gate is manifest declared:
   `A` is a subset of `M`. Manifest grantability and configured egress may take
   the values ADR 0056 and the deployment allowlist permit.

3. **Permissive** matches every remaining valid installation. In particular,
   an installation with an operator gate outside `M` is Permissive, because it
   has an armed gate whose approval provenance the bundle does not declare.

The order makes overlap intentional. A normal installation with no grantable
gate and no egress matches both Strict and Standard, and resolves to Strict.
A normal installation with manifest only gates, configured egress, and manifest
grantability resolves to Standard. An installation with an additional operator
gate resolves to Permissive unless Strict matched first.

An installation that fails ADR 0050 validation or cannot load its policy has no
posture result because it does not have an effective policy to classify. A
resolver reports that configuration error rather than assigning a name.

## Consequences

1. A resolver can hand compute one posture name from its inputs without relying
   on implementation specific precedence.
2. The name `declared` always identifies the manifest source and never hides
   the operator half of ADR 0050's union.
3. The three names remain reporting only. This ADR authorizes no resolver,
   enforcement, or configuration change.
4. ADR 0088 remains an immutable Accepted record even though its body says it
   remains Draft until maintainers approve the identity modes.

## Alternatives considered

1. **Keep the three names but let a resolver choose either matching name.**
   Rejected because two implementations can report different names for the same
   enforced policy.
2. **Make Standard and Permissive differ by a new grantability or egress setting.**
   Rejected because it would invent an enforcement axis that ADR 0104 did not
   establish.
3. **Treat operator gates as manifest declared.** Rejected because it erases
   the source distinction ADR 0050 explicitly preserves through its union.
4. **Implement a resolver with this ADR.** Rejected because the decision is
   cheap to correct before any consumer reads a posture name. Implementation
   follows only after this Draft is Accepted.
