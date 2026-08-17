# 102. Accepted alongside implementation with explicit approval

Date: 2026-08-11

Status: Accepted

**Amends [ADR-0085](0085-acceptance-not-implementation-authorizes-an-adr.md)**
by replacing Decision clause 3. All other clauses and the history rules remain
unchanged.

## Context

ADR 0085 correctly separates maintainer acceptance from implementation evidence.
Its third clause is too strict for an explicitly approved change whose realizing
code path is landing at the same time. This clause was violated for six
consecutive review windows, and ADR 0101 landed Accepted in implementing commit `5d20f547`. A
status transition gate was considered and rejected because the approval decision,
not a doclint workflow, is the authority.

## Decision

An ADR may be published as Accepted alongside its implementation only when
explicit maintainer approval is recorded and the ADR names the code path that
realizes its decision. The implementation does not itself authorize acceptance.
Without both approval and a named realizing code path, implementation must wait
until acceptance.

## Consequences

Maintainers may accept and implement one decision in a coordinated change while
keeping approval visible and auditable. The repository does not gain a status
transition gate, and code evidence remains supporting evidence rather than
authority.

## Alternatives considered

1. Require acceptance before every implementation. Rejected because it blocks a
   coordinated, explicitly approved change.
2. Gate status transitions with doclint. Rejected because a workflow cannot
   replace explicit maintainer approval.
3. Let implementation evidence authorize acceptance. Rejected because it
   reverses the authority this amendment preserves.
