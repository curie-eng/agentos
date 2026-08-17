# 103. Previous schema shape gate

Date: 2026-08-11

Status: Accepted

**Amends [ADR-0101](0101-schema-compatibility-for-closed-schemas.md)**
by replacing Decision section 4. All other sections remain unchanged.

## Context

ADR 0101 called for comparing a sample payload with the previous schema
revision. Issues #1056, #1057, and #1306 were property additions. The shipped
protection addresses that class directly through its realizing code path,
`cli/tests/schema_inventory.rs`. Only 23 of 47 schema families have a sample,
so a payload gate cannot be universal. Issue #1430 records executed evidence of
what the gate catches and misses.

## Decision

The gate in `cli/tests/schema_inventory.rs` uses a name based comparison of
`properties` and `required` against the previous committed revision. It fails
for an unchanged identifier when either shape changes. Type changes, enum
narrowings, and const flips are explicitly out of scope for this gate.

## Consequences

Property additions and required membership changes are caught across every
schema family, including families without a sample payload. Payload comparison
and broader semantic comparison are rejected or deferred because they require
coverage and policy beyond this gate.

## Alternatives considered

1. Compare a sample payload against the previous revision. Rejected because
   only some schema families have samples.
2. Compare all semantic schema changes. Deferred because this amendment targets
   the demonstrated property shape defect class.
3. Leave the current schema only gate unchanged. Rejected because it cannot
   detect an unchanged identifier with a changed previous shape.
