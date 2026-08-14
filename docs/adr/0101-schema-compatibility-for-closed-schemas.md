# 101. Closed schemas version on every change: minor for optional, major for required

Date: 2026-08-10

Status: Accepted

**Amended by [ADR-0103](0103-previous-schema-shape-gate.md)**
(back link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
the gate described in Decision section 4 is overtaken. The shipped gate uses a
name based comparison of `properties` and `required` against the previous
committed revision. Type changes, enum narrowings, and const flips are out of
scope. The rest of this ADR stands.

Implements [#1075](https://github.com/curie-eng/curie/issues/1075).

**Amends ADR-0074** ("Versioned JSON Schemas for every agent-facing CLI result").
ADRs are immutable once Accepted, so this is a new ADR that **supersedes in part**
exactly one clause of 0074's Decision: item 5's **Additive (compatible) -- no
version bump**. Everything else in 0074 stands unchanged: one schema per
agent-facing result family, the inventory index as the single source of truth,
the `syn`-based gate over every `impl CliOutput`, schemas embedded in the
published binary and printed by `curie schema-index`, and the breaking-change
half of the compatibility policy.

## Context

0074's additive clause justifies itself on consumer behavior:

> Consumers following the repo's superset-JSON convention (ignore unknown keys)
> keep working. The schema file is edited in place at `vN`.

Every committed schema contradicts that justification. All 39 declare
`"additionalProperties": false`, so a consumer holding a cached copy of the
previous revision does NOT ignore an unknown key -- it rejects the whole payload.
Followed correctly, the additive clause therefore produces breaking changes at an
unchanged `$id`, which is the one thing 0074 exists to prevent ("a breaking output
change can no longer land silently").

Measured across all 39 schemas at the time of writing, walking each file's history
back to the earliest revision carrying today's `$id` (the AgentOS to Curie rename
changed the `$id` host, so an earlier revision is a different identifier and not a
compatibility question), **eight** have gained keys at an unchanged `$id`:

| schema | added at an unchanged `$id` |
| --- | --- |
| `diff` | `chart_deployed`, `chart_target`, `chart_version_differs`, `unresolved_credentials` -- all four also `required` |
| `doctor` | `guidance` -- also `required` |
| `approvals` | `routes` |
| `guide` | `approvals` |
| `eval` | `bundle_digest` |
| `status` | `bundle_digest` |
| `sweep` | `bundle_digest` |
| `message` | `status` |

The two that added a `required` key break in BOTH directions: a consumer holding
the old revision rejects a new payload for the unexpected key, and a consumer
holding the new revision rejects an old payload for the missing required one.

Three separate instances landed without a red build (#1056, #1057, #1306) because
no gate compares a payload against the PREVIOUS revision of its own `$id`;
`json_contract.rs` validates the new payload against the new schema, so the class
is invisible by construction.

The pain is asymmetric, and that asymmetry is what this ADR turns on. A new
optional key breaks only a consumer holding a stale copy. A new required key
additionally breaks a consumer holding the current copy but reading an older
payload -- a producer/consumer skew that no cache invalidation fixes.

## Decision

**A closed schema versions on every shape change. Adding an optional property is
a MINOR bump; anything a conforming consumer previously accepted and no longer
does is a MAJOR bump.**

### 1. The bump rule replaces 0074's additive clause

- **Minor (`vN` to `vN.M+1`).** Adding a new OPTIONAL property, a new `oneOf`
  branch, or a new enum value. A consumer on the previous revision would reject
  the new payload, so the identifier has to move; a consumer that refetches is
  whole again, and no payload it previously produced becomes invalid.
- **Major (`vN` to `vN+1`).** Everything 0074 already called breaking -- removing
  or renaming a property, changing a type, promoting an optional property to
  required, tightening a domain -- PLUS introducing a property that is `required`
  at birth. Both invalidate a payload a conforming consumer previously produced.

`additionalProperties: false` stays. It is load-bearing: it is what makes a
dropped or misspelled key a test failure rather than silence, and it is the
premise the emit-parity gate's projections rely on. The closed world is not the
defect; versioning that pretended a closed world was open was.

### 2. Prefer optional over required for new properties

0074 is silent on this and the schemas drifted strict. A new property that a
consumer can reasonably tolerate as absent SHOULD be optional, which keeps its
addition a minor bump. `required` is for properties whose absence makes the
payload unusable. This is a default, not a prohibition: a genuinely mandatory
property is still declared `required` and still costs a major bump.

Existing `required` sets are NOT retroactively relaxed. Removing a property from
`required` is itself a compatibility event under this ADR's own rule, so a
wholesale loosening pass would be exactly the kind of unversioned churn this ADR
exists to stop.

### 3. The identifier carries both numbers

The `$id` filename segment becomes `vN.M`, and `index.json`'s `version` becomes
the same string:

```
"$id": "https://schemas.curietech.ai/cli/guide/v1.1.json"
"version": "1.1"
```

`vN` with no minor is read as `vN.0`, so today's `v1` is `1.0` and no schema has
to move until it changes. The index's `version` MUST still equal the `$id`'s
version segment, which is the invariant the inventory gate already enforces; only
its type widens from integer to string.

The `$id` is the cache key. An agent discovers a schema through `curie
schema-index <name>` and keys what it caches by `$id`, so moving the identifier is
not decoration -- it is the whole mechanism by which a stale consumer learns to
refetch. That is why a minor bump is a real fix and not bookkeeping.

### 4. A gate that compares against the previous revision

`json_contract.rs` proves a payload matches its CURRENT schema, which cannot see
this class. A new gate validates each committed schema's sample payload against
the PREVIOUS committed revision of the same file, and fails when the payload does
not validate while the `$id` is unchanged. Its verdict is a direct reading of this
ADR: unchanged `$id` plus a payload the previous revision rejects is the defect.

## Consequences

- Every shape change now moves the identifier, so "my cached schema rejects valid
  output" stops being reachable. That is the guarantee 0074 claimed and did not
  have.
- Eight schemas are brought into compliance in the implementing change: six that
  added optional properties go to `v1.1`, and `diff` and `doctor` go to `v2`
  because their additions were `required`. This is a one-time cost of having
  followed the superseded clause correctly, not a defect in any of those PRs.
- A minor bump is now cheap and expected, so the pressure that made "no bump"
  attractive is gone. The cost of an optional field is one digit and one index
  line.
- A consumer pinning `v1.0` keeps working against a `v1.0` payload forever; it
  simply does not see fields added later. A consumer wanting new fields refetches.
  Neither silently mis-validates, which is the property that was missing.
- The gate can only compare against the immediately previous committed revision,
  not every historical one. A change that is individually clean but breaks a
  consumer two revisions back is still possible. Accepted: the identifier moves on
  every change under this ADR, so a two-revision-old consumer is already on a
  different `$id` and refetches for that reason rather than mis-validating.
