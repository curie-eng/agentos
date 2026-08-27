# 132. Gate evals on the worst cohort and require cross-family verifiers

Date: 2026-08-27

Status: Draft

This Draft records the decision proposed by
[#1616](https://github.com/curie-eng/curie/issues/1616). It authorizes no
implementation unless a maintainer accepts it first. If accepted, it extends
[ADR-0019](0019-freeze-eval-case-format.md),
[ADR-0022](0022-eval-completeness-tier-parity-and-trace-promotion.md), and
[ADR-0042](0042-llm-as-a-verifier-grader-and-progress-signal.md); amends
[ADR-0037](0037-opt-in-binding-hook-and-pareto-model-routing.md) as stated below;
and, if [ADR-0128](0128-install-owned-model-gateway-and-agent-aliases.md) is
accepted, narrows its automatic-alias rule for verifier evals. It is bounded by
[ADR-0055](0055-the-fake-model-is-a-plumbing-fixture.md).

## Context

The shipped GitHub check is green exactly when `total > 0` and
`passed_count == total`. `total` counts every result row, including an ungraded
row; all-plumbing runs are not reported under ADR-0055. That predicate is already
stricter than an overall binary pass rate and must not be weakened.

There is nevertheless no cohort policy and no score-carrying result from which
to construct one. `ScoreResult` carries a Boolean and detail, `EvalCaseResult`
carries an outcome but no normalized score, and multi-sample aggregation discards
its pass fraction into prose. With only Boolean `1`/`0` scores, the existing
all-cases predicate forces every cohort to `1`, so a cohort gate adds no decision.
ADR-0042's continuous verifier score makes the intended control reachable: cases
may each clear their case threshold while a weak cohort remains below its higher
release floor and the overall mean clears that floor.

An LLM verifier can share blind spots with the system under test. Requiring a
different model family is a fail-closed decorrelation guard. It is not proof of
statistical independence or correctness: different families can share training
data, distillation lineage, and systematic errors.

The design is informed by ANIBIT14/zolva's `EvalRunner` and worst-cohort report
(`src/zolva/evals.py`), an Apache-2.0, four-star project when #1616 was filed.
This is design-only borrowing: Curie takes no dependency and copies no code.

## Decision

### A normalized case score precedes cohort aggregation

The scorer result contract first gains a required `normalized_score` in `[0,1]`
for every graded real-model sample. A deterministic scorer returns `1` or `0`;
ADR-0042's verifier returns its continuous `R`. `EvalCaseResult` carries the
score that corresponds to its binary outcome. For multi-sample cases, the score
is the arithmetic mean of the graded sample scores while the existing
majority/pass-at-k rule still decides the binary outcome. Cases, not samples,
are equally weighted in a cohort.

`plumbing_ok` alone has `normalized_score = null`. A missing, non-finite, or
out-of-range score on a graded result is an error, as is a non-null score on a
plumbing result. No cohort aggregation code may ship or advertise capability
until this result contract is present at every tier.

[#389](https://github.com/curie-eng/curie/issues/389) is already shipped and
remains the deterministic whole-suite `TrajectoryScorer` path selected by
`trajectory.json`; it returns normalized `1`/`0` without changing its selection
or `case_id -> TrajectorySpec` contract. A suite with that sidecar may not contain
an ADR-0042 verifier case, because the whole-suite scorer would bypass the
per-case verifier. Such a mixture is rejected rather than silently choosing one.
The ordinary grader path may mix deterministic and verifier cases once
ADR-0042's per-case verifier dispatch exists; every verifier consumes the full
trajectory ADR-0022 requires.

### Cohort membership is portable; release floors are trusted policy

In a separately reviewed ADR-0019 schema change, `EvalSuite` gains a non-empty
set of cohort names and `EvalCase` gains a cohort name. A cohort-enabled suite
assigns every case to exactly one declared cohort. Duplicate names, partial
assignment, unknown names, duplicate case ids, or empty declared cohorts are
invalid. These optional additions regenerate
`apps/worker/schema/eval-cases.schema.json`, update the Rust mirror and shared
conformance fixture, and pass the existing drift gate.

The bundle does **not** set release floors. A versioned `EvalGatePolicy`, outside
the bundle, maps required cohort names to floors in `[0,1]` and says whether the
target requires cohort-capable reports. For a GitHub release check, the
installation-owned release-target binding identifies the policy artifact, and
the API resolves it at the protected base SHA, never at the candidate head. That
resolved snapshot is the sole authority and its identity and digest are stamped
into the job. A candidate-repository policy change therefore runs under the
previously trusted policy, needs normal review and branch protection, and becomes
authoritative only for subsequent candidates. The candidate cannot lower its own
floor, collapse cohorts, or omit cohorts to green itself.

A target whose trusted policy does not yet require cohorts may continue to run a
legacy suite. Once the trusted policy requires cohorts, a legacy or partially
cohorted suite is a configuration failure, not a fallback to the legacy report.
Developer previews may select a proposed policy explicitly, but that preview is
not release-authoritative and cannot post the protected GitHub context.

### The gate is a conjunction decided by the worst cohort

For a cohort-capable real-model report, release success requires both:

1. the exact shipped predicate, `total > 0 && passed_count == total`; and
2. every policy-required cohort is present, non-empty, covers exactly its
   assigned graded cases, and has `mean(case.normalized_score) >= floor`.

The overall mean is reporting only. The minimum `score - floor` margin is the
cohort decision, so strength elsewhere cannot offset the weakest cohort.
Unknown or extra policy/cohort names, duplicate coverage, missing cases, invalid
scores, a policy-digest mismatch, or inconsistent derived values fail closed.

ADR-0055's ordering remains load-bearing. A case must first pass the existing
completion/status gate, then a fake-model case returns `plumbing_ok` before any
scorer, family lookup, or verifier call. An all-plumbing run emits no quality
report; a cohort-enabled fake run follows this plumbing-only exception and never
emits V2 or cohort scores. A fake completion/status failure retains ADR-0055's
genuine red V1 path. A cohort-capable report containing any plumbing row is
invalid: cohort coverage is only over graded real-model results.

### Cohort red uses a distinct, fail-closed report capability

The existing `EvalReport` cannot express a cohort red honestly, and adding an
optional control-bearing field would let an old API ignore it and post success.
Instead a separately reviewed ACI change introduces a distinct, versioned
`EvalGateReportV2` message and API route. A conforming V2 producer never calls the
V1 route; an old API has no V2 route, so capability preflight or delivery fails
rather than entering V1's tolerant parser. The new shape has a
required version/capability discriminator, trusted-policy id and digest, all
case ids/outcomes/normalized scores/cohort assignments, legacy counts, cohort
summaries, and the derived gate decision. Empty or partially populated V2 is
invalid. The frozen ACI change receives the required semver bump and synchronized
Python, JSON Schema, TypeScript, and Rust artifacts.

The API recomputes counts, coverage, scores, margins, and the conjunction from
the required rows and its trusted policy; `github_checks.py` uses that recomputed
decision and names the worst cohort, score, floor, and case count. It rejects a
producer's inconsistent decision. The API can validate structure and arithmetic,
but it cannot prove that the worker honestly obtained a semantic score or called
the stamped model. Authenticated producer identity, trace evidence, and the live
verification lane provide that evidence boundary. `EvalGateReportV2.target_url`
remains only the optional GitHub status link.

Rollout is consumer first:

1. land the normalized scorer/case-result contract and its tier-parity vectors,
   without cohort gate behavior;
2. land the reviewed V2 ACI contract, V2 API route, GitHub consumer, CLI reader,
   and an advertised `eval-gate-report-v2` capability;
3. land the ADR-0019 cohort schema and gate-policy schema, then the worker/CLI
   producer and aggregator; a real-model producer preflights the V2 capability
   and never sends a cohort-enabled run as V1; fake runs follow ADR-0055's
   plumbing-only exception; and
4. enable a trusted cohort policy only after every required consumer is V2.

Legacy suites keep using V1 and its exact all-cases predicate. No V2 producer is
enabled while a consumer can ignore cohort red.

### Verifier family identity is installation-owned and fail-closed

If accepted, this ADR amends ADR-0037's install-level model descriptor to add a
required canonical `family` for every concrete real model usable as a system
under test or verifier. Family is model lineage, not provider, endpoint, price
table substring, request text, or a token parsed from a model id. The canonical
underlying model/deployment identity maps to exactly one family; aliases point to
that descriptor and cannot relabel it. Different deployments may share a family,
but the same identity cannot acquire two family labels.

The worker compares the registry-resolved system and verifier descriptors only
for real-model cases whose grader is `verifier`. It does so after ADR-0055's fake
plumbing disposition and before verifier execution. Missing or unknown model or
family metadata, equal canonical families, or an unverifiable alias is a clear
configuration error and no verifier call occurs. This ADR newly requires the
system model id/family, verifier family, registry revision, policy digest, and
report version in the run config hash and report; ADR-0042 already requires the
verifier model, `G`, `K`, and criteria stamp.

If ADR-0128's automatic gateway alias remains unresolved until the first model
request, it is forbidden for either side of a verifier suite. It is allowed only
when the gateway/registry pins and attests the concrete descriptor and family at
preflight; a requested alias alone is not identity. The `EvalJob.target_url`
shortcut (the runner base URL, not the report's GitHub link) likewise cannot
self-assert a system family: it must return an authenticated descriptor attestation
or verifier grading is rejected.

Cross-family checking does not make an unreachable verifier real. Cohort gating
cannot be enabled until ADR-0042's model-plane validation has demonstrated
credential and egress delivery plus either scoring-token logprobs or its reviewed
two-stage workaround, with useful agreement and non-flapping variance. The
implementation PR must carry a disposable, budget-capped live-provider run using
two registered families; a later nightly lane is regression coverage, not a
substitute for that pre-merge proof.

### Tier parity and agent-facing schemas move together

Given identical suite, case results, and trusted policy bytes, `skill`, `local`,
and `cluster` must emit the same gate decision and worst-cohort summary. Python is
the source of truth; the Rust fallback mirrors the pure aggregation contract and
is locked to it by shared positive, negative, malformed-input, and mutation
vectors as ADR-0019/0022 require. Local and cluster use the worker decision rather
than re-authoring it in command handlers.

The cohort additions change the frozen eval-case schema and Rust mirror. The
score/result and V2 report changes update every result DTO they cross. Adding
required cohort decision fields to the closed agent-facing `eval --json` output
is a major schema bump under ADR-0101, with its index, sample, and emit-parity
artifacts changed in the same reviewed commit. A CLI that does not understand the
V2 capability must reject it, never ignore cohort fields.

## Verification required before completion

- Through the real V2 API and GitHub reporter, all binary cases pass, two
  equal-floor cohorts produce an overall mean above the floor, and the weak
  cohort remains below it; the posted state is `failure`. A mutation run changes
  the canonical production decider from minimum margin to overall mean and shows
  the identical request false-green. The mutant is generated for the test and is
  not a second checked-in decider.
- Through the real V2 API and GitHub reporter, candidate-head attempts to lower
  a required floor or delete a required cohort yield the same base-resolved
  decision as the unmodified candidate and stamp the same protected-base policy
  id/digest; a policy-digest mismatch is rejected without posting a GitHub
  status.
- A separate cohort-enabled fixture has a binary failing case while every cohort
  remains above its floor. Removing the legacy conjunct makes that fixture
  false-green, proving the old all-cases rule independently.
- The real verifier preflight rejects same-family, missing-family, conflicting
  identity/family, unknown-family, unattested `auto`, and unattested
  `EvalJob.target_url` configurations. Removing the family comparison makes the
  same-family test fail; a registered different-family pair reaches a real
  verifier and records both attestations.
- The fake tier proves the done gate still runs, returns `plumbing_ok` before
  family resolution, and never emits V2. Skill, local, and cluster conformance,
  frozen-schema/codegen checks, API validation, CLI schema/type checks, lint, and
  the affected tests all pass.

## Consequences

- A release can be red while every thresholded case is green, and its status
  explains the weakest cohort without falsifying `passed_count`.
- Cohort release policy is review-controlled and cannot be weakened by the
  candidate it judges.
- V2 requires an ordered capability cutover rather than a fail-open rolling
  field addition. Legacy V1 remains exact for suites not required to use cohorts.
- Canonical family metadata and preflight attestation reduce correlated-judge
  risk but deliberately make no claim of verifier independence.

## Alternatives considered

- **Gate on the overall average.** Rejected because a strong cohort can hide a
  weak safety or reliability cohort.
- **Let the bundle set floors or force red by changing `passed_count`.** Rejected
  because the candidate would parameterize its own gate or the report would lie
  about case outcomes.
- **Add optional V1 cohort fields.** Rejected because an old tolerant consumer
  can ignore a control-bearing red.
- **Infer family from provider or model names, or merely warn.** Rejected because
  aliases and gateways defeat inference and unknown identity must not green a
  verifier run.
- **Depend on or copy zolva.** Rejected because a design precedent does not
  justify coupling Curie's frozen contracts or runtime to that project.
