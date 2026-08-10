# Schema compatibility baseline

The last **published** revision of every committed schema, snapshotted so the
ADR-0101 compatibility gate has something to compare against.

Why a committed snapshot rather than `git show HEAD~1:...`: CI checks out at
depth 1. A history-based gate would find no previous revision, skip every
schema, and pass -- a gate that is green because it did nothing, which is the
exact class ADR-0101's context describes. A file in the tree is present at any
clone depth.

## Reading a failure

The gate compares identifiers first. It only demands compatibility when the
`$id` has NOT moved, because a moved `$id` is the fix: an agent keys what it
caches by `$id`, so a bump is what makes it refetch.

- `adds [...]` at an unchanged `$id` -- a consumer holding that identifier
  rejects the new payload, since every schema is `additionalProperties: false`.
  Bump the **minor** (`v1` -> `v1.1`).
- `newly requires [...]` -- that consumer also rejects an OLDER payload, which
  no refetch fixes. Bump the **major** (`v1` -> `v2`).

## Updating it

```
curie dev schema-baseline
```

Run it after a schema change has been versioned, and only then. The script
refuses when any schema changed shape while keeping its `$id`, and exits early
when nothing changed -- running it speculatively is how the baseline stops being
a record of the last published revision and quietly becomes a copy of whatever
is checked out, costing the gate its reference point.
