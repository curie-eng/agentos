# 85. Acceptance, not implementation, authorizes an ADR

Date: 2026-07-29

Status: Accepted

**Amends [ADR 0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md).**
This ADR replaces only these two clauses in ADR 0045:

1. Decision clause 1 at lines 60 through 62, which permits a status correction
   when a Proposed decision has shipped.
2. The Decision status evidence paragraph at lines 95 through 100, which makes
   implementation evidence the authority for promotion to Accepted.

Every other rule in ADR 0045 remains in force.

## Context

ADR 0045 makes implementation evidence the authority for acceptance. That
couples two separate decisions: whether maintainers approve an architectural
direction and whether its implementation is complete.

The distinction matters as the repository becomes more open. Contributors need
to merge Draft ADRs for visible discussion without creating an authorization to
build them. Maintainers also need to accept a direction before implementation
starts so issues and pull requests can work from an approved architectural
constraint.

Implementation evidence remains useful for auditing the corpus. It cannot be
the decision authority because code may exist before review, may implement only
part of a decision, or may have landed without awareness of the ADR.

## Decision

The active authoring statuses are `Draft` and `Accepted`. `Superseded` remains
the terminal historical status.

1. A `Draft` ADR may merge for discussion. It remains open for revision and
   cannot authorize implementation.
2. An ADR becomes `Accepted` only when its Accepted status is published with
   explicit maintainer approval.
3. Implementation may start only after acceptance. The implementing GitHub
   issue and pull request link the Accepted ADR.
4. Existing implementation cannot retroactively accept a Draft. Partial or
   complete implementation is audit context, not acceptance authority.
5. Acceptance approves the decision. It does not claim that implementation is
   started or complete. Issues and pull requests carry that state.

This amendment preserves the history rules in ADR 0045:

1. Once an ADR is Accepted, its context, decision, alternatives, consequences,
   evidence, and citations remain immutable.
2. An Accepted ADR may change only through its status line, a superseded by back
   link, or a same referent pointer repair. The status line may change to
   `Superseded` only when the decision has been replaced in whole.
3. Partial supersession keeps the older ADR `Accepted` and adds the precise back
   link required by ADR 0045.
4. A changed decision requires a new ADR. The new ADR records the supersession,
   and the older reasoning is never rewritten.

## Consequences

1. A merged Draft is a discussion artifact with no implementation authority.
2. Maintainer approval is visible before implementation begins.
3. Decision status and delivery status no longer stand in for each other.
4. Implementation evidence still supports corpus audits but never changes
   status by itself.
5. ADRs 0060 and 0079 become Accepted through explicit maintainer
   approval in this migration. Their existing implementation evidence does not
   create a precedent for future acceptance.

## Alternatives considered

1. **Keep implementation evidence as the acceptance authority.** Rejected
   because it accepts decisions after code exists and lets implementation bypass
   architectural approval.
2. **Keep Draft ADRs out of the repository.** Rejected because visible review
   and open discussion benefit from merged Drafts.
3. **Allow work to begin from a merged Draft.** Rejected because discussion
   would become implicit authorization and rejected directions could acquire
   implementation momentum.
4. **Retain Proposed as a third active status.** Rejected because Draft already
   represents every architectural decision that lacks maintainer acceptance.
