# 135. Langfuse stays the shipped trace store; the cost, and what would reopen it

Date: 2026-08-29

Status: Draft

Raised by [#2055](https://github.com/curie-eng/curie/issues/2055).

Records a decision the swap-readiness table in
[`docs/architecture-vision.md`](../architecture-vision.md) already grades but never
settles: the observability row is graded, the cheapest next step is named, and
nothing says whether we intend to act on it. [ADR 0016](0016-swappable-jobs-around-an-opinionated-core.md)
governs the rejected alternative, and [ADR 0004](0004-langfuse-observability-and-eval-backbone.md)
is the adopt decision this one revisits rather than replaces.

## Context

**What the product actually reads.** The whole read surface funnels through one
client, and the routes on top of it fan out across the Runs proxy plus the
observability, evals and cost routers:
`apps/api/src/curie_api/routers/observability.py:31` and `:43` for the two metrics
routes, `apps/api/src/curie_api/routers/evals.py:106` for the eval matrix (calling
`build_matrix` at `:121`), and `GET /cost` at
`apps/api/src/curie_api/routers/control.py:169-191`, which per `control.py:5`
"composes the OB1 metrics module filtered to the agent". That is exactly what
`docs/architecture-vision.md:315` means by "read side spans several API modules plus
routers". The one client, `apps/api/src/curie_api/langfuse.py:163`, calls itself a
"Thin async client over Langfuse's public read API" and is 242 lines; its trace-list
filter is not even server-side, since `langfuse.py:19-22` scans a bounded,
newest-first page and matches in our own process because "Langfuse's list API has no
substring filter", capped at `_TRACE_SCAN_LIMIT = 500`. `build_tree()` at
`langfuse.py:125` rebuilds the nested observation tree from a flat list, again in our
code. The proxy is `apps/api/src/curie_api/routers/runs.py:1`, "Langfuse read proxy
powering the Runs view", three routes under the `/langfuse` prefix declared at
`runs.py:14-18`. The only ClickHouse-featured read is
`apps/api/src/curie_api/metrics.py`, 255 lines, assembling five aggregates
(`SCALAR_METRICS` at `metrics.py:35` plus `error_rate` at `metrics.py:36`) in
`summary()` at `metrics.py:151`. The eval matrix touches no scores at all:
`apps/api/src/curie_api/evals.py:1-9` reads "the outcome straight off each suite
trace's metadata", from the `version:` and `suite:` tags, "rather than joining a
globally-paginated scores query".

**What the rail costs to install.** Langfuse already reuses the shared Valkey
(`charts/curie/templates/_helpers.tpl:618`), so that is not incremental. What is
incremental is ClickHouse: `charts/curie/templates/clickhouse.yaml` is 148 template
lines with a values block spanning `charts/curie/values.yaml:317-424`, plus
`charts/curie/templates/langfuse-model-pricing.yaml` at 104 lines seeding the model
price rows so cost is not reported as zero, plus
`charts/curie/templates/preflight-avx.yaml` at 110 lines existing solely for this
store. That preflight is the sharpest evidence: newer ClickHouse "is compiled for
AVX and SIGILLs with exit 132 on CPUs that only have SSE4.2", so the hook reads the
node's `/proc/cpuinfo` "and if the node lacks AVX it FAILS the install"
(`charts/curie/templates/preflight-avx.yaml:1-11`). ADR 0004 predicted this gotcha
in its Consequences; it is now shipped chart surface.

**What the increment buys.** Four things the read surface above does not cover. The
bundled trace/cost/score web UI as a first-class shipped endpoint, named "Langfuse UI
(traces / cost / evals)" at `cli/src/observability.rs:116`. The memory-provenance
deep-link target, `_trace_url()` at
`apps/api/src/curie_api/routers/memory.py:132-135`, "A Langfuse deep link for a source
trace id (the trace-back target)". The only rendering of `eval_pass` scores: the
worker writes the score at `apps/worker/src/curie_worker/eval/recorder.py:142` (under
`SCORE_NAME`, defined at `recorder.py:27`) and no production code path reads it back.
Every other first-party reference to it is write-side or prose: the re-export at
`apps/worker/src/curie_worker/eval/__init__.py:19`, and the eval-matrix docstring at
`apps/api/src/curie_api/evals.py:5`, which is the matrix explaining that it reads
metadata instead of the score. With no first-party reader, the bundled UI is the only
thing that renders it. And the cost engine itself, the price-row matching
`_cost_known()` documents at `metrics.py:180-190`: Langfuse "returns a generation's
cost by matching its model to a stored price row; with no matching row it returns 0
even when tokens were spent".

The disproportion is real, and the repo already graded itself honestly on it:
`docs/architecture-vision.md:315` marks observability "B+", write side clean but for
three vendor span attributes and read side spanning several API modules plus routers;
`docs/architecture-vision.md:316` marks evals "B", leaving the `version:`/`suite:` tag
convention unfrozen; `docs/architecture-vision.md:109` concedes the read logic "would
be rewritten, not ported". Those three attributes are frozen into an otherwise-neutral
vocabulary at `packages/telemetry-schema/src/curie_telemetry_schema/__init__.py:15-17`.

## Decision

**Langfuse stays the shipped trace store.** The disproportion above is accepted
deliberately, not overlooked.

The obvious alternative -- a thinner store sized to the five aggregates and the tree
we actually read -- is not merely unattractive. It is **barred by an existing
decision.** ADR 0016 holds that a port "is promoted from convention to a frozen
contract only when a real swap demand arrives", and that a PR adding a speculative
abstraction ahead of a real second implementation violates that ADR "even when the
code is clean". No such demand is recorded, so building the thin store today is
exactly the negative work 0016 names.

Related open issues #1645, #1765, #1817, #1818 and #1819 are operability items.
None of them records this keep-versus-swap decision, which is why it needed its own
ADR rather than a comment on one of them.

## The revisit trigger

This decision reopens when either of the following is observed, and a future reader
can check both:

1. **Install-footprint failures the tag pin cannot absorb.** Repeated install
   failures or operator complaints on no-AVX or arm64 node fleets that pinning an
   SSE4.2-safe ClickHouse tag does not resolve. The preflight proves this pain is
   live, not hypothetical: a node without AVX fails the install outright today.
   "Repeated" means more than one distinct operator or fleet, not one awkward
   install.
2. **A customer-demanded second trace store.** Somebody's deployment requires
   traces in a store we do not ship. This is precisely the "real swap demand" ADR
   0016 waits for, and its arrival is what would make the thin-store work
   legitimate rather than speculative.

Absent both, the disproportion is not by itself a trigger. It is the accepted cost.

## Deferred options, costed, none authorized

Listing these here does **not** authorize any of them. They are recorded so a later
reader inherits the costing rather than redoing it; each still needs its own ticket
and, where it changes a contract, its own decision.

- **(a) Map the three `langfuse.*` span attributes to neutral names in the
  collector.** Cost: a collector-side rename plus a change to the frozen
  `TelemetryAttr` vocabulary at
  `packages/telemetry-schema/src/curie_telemetry_schema/__init__.py:15-17`. Buys the
  write-side grade `docs/architecture-vision.md:315` already names, and nothing the
  product reads today.
- **(b) Rename the `/langfuse/*` API routes** (`apps/api/src/curie_api/routers/runs.py:15`).
  Cost: this is `--json` DTO and route surface, governed by
  [ADR 0101](0101-schema-compatibility-for-closed-schemas.md) (closed schemas version
  on every change) and [ADR 0103](0103-previous-schema-shape-gate.md) (the previous
  schema shape gate). It is a versioned, gated schema change, not a rename.
- **(c) Mark the `eval_pass` score write** at
  `apps/worker/src/curie_worker/eval/recorder.py:142` **as UI-only rather than dropping
  it.** Nothing first-party reads the score back, the matrix included: it reads tags
  and metadata (`apps/api/src/curie_api/evals.py:1-9`). But the score feeds the
  self-hosted score views at zero marginal cost, so the correct disposition is to
  label it, not delete it.

## Consequences

Every install keeps paying the ClickHouse footprint, the AVX preflight and the
price-row seeding, whether or not the operator opens the bundled UI. That is now a
recorded choice, so a reviewer who rediscovers the disproportion finds this ADR
instead of filing it again.

The three vendor attributes stay frozen in the telemetry vocabulary and the
`/langfuse/*` routes stay in the public CLI and API surface. Both are named above as
deferred, so their presence is not evidence that nobody noticed. The vision doc's B+
and B grades likewise stand as written: this ADR does not raise them, it records that
we read them, priced the fix, and declined it for now.

Cost figures stay only as accurate as the seeded price rows. A model with no matching
row reports zero spend against non-zero tokens, and `_cost_known()` is the only thing
between that and a wrong number on the dashboard.

## Alternatives rejected

**Build a thin first-party trace store sized to what we read.** Rejected as barred
by ADR 0016 rather than merely as effort: there is no second implementation and no
recorded swap demand, so the interface would encode guesses. It would also have to
reimplement the cost engine, the score views, and the deep-link target, which are
the parts we get for free and the parts nobody has scoped.

**Do the cheapest-next-step renames now** (options (a) and (b) above). Rejected as
sequencing: they improve a grade in a table without changing what any deployment
installs, and (b) drags a gated schema version along with it. If the trigger fires
they become prerequisites and get done then, with a swap depending on them.

**Drop the `eval_pass` score write because no first-party reader consumes it.**
Rejected: it costs nothing to keep and it is the only thing populating the
self-hosted score views. The gap is that its status is undocumented, which option
(c) fixes by labelling it.

**Leave it unrecorded and let the vision doc's grade speak for it.** Rejected because
a grade is an assessment, not a decision: the table says the seam is B+ and names a
cheaper shape, but not that we are keeping the current one, why, or what would change
our mind. That is the gap this ADR closes.
