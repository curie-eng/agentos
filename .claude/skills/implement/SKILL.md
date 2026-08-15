---
name: implement
disable-model-invocation: true
description: Implement a ticket or feature with proportional planning, test first behavior changes, independent review when available, and verification through the real affected surface.
---

# Implement

**Usage:** `/implement [ticket URL, ID, or description]`

Use this as the default entry point for a ticket with clear acceptance criteria.
Use the lightest process that can prove the requested behavior. Stop and ask for
direction when the ticket cannot be read, its acceptance criteria are materially
ambiguous, or implementation would require a decision outside the ticket.

## Portable provider policy

Use the active environment for implementation. Do not require a particular agent,
model, CLI, plugin, or hosted service.

If another independent provider is available locally, use it for a read only plan
or diff review. Claude, Codex, and OpenCode are examples, not requirements. Keep
execution with one provider and use another only to challenge the plan or completed
diff. When no independent provider is available, use a fresh reviewer context in
the active environment and state that the review was single provider.

Never install, configure, authenticate, or invoke a provider solely to satisfy this
workflow. Never depend on home directory paths, custom profiles, personal hooks,
metrics, queues, databases, background jobs, private credentials, or machine local
helper scripts. Repository instructions and available tools are authoritative.

## Setup

1. Read the ticket and its linked requirements. Do not infer scope from an ID alone.
2. Read the repository instructions and the relevant architecture or component docs.
3. Check for an existing branch or worktree for the ticket. If one exists, read
   `.projects/plans/<branch-name>.state.json` inside it and resume from the stage
   it records rather than repeating finished work. See Run state below. Otherwise
   select the target release train before creating work:

   | Work | Base and PR target |
   | --- | --- |
   | General bug, security fix, or shared change | `main` |
   | v0.7 feature or a bug unique to unreleased v0.7 work | `next` |

   Fetch the chosen base and create the worktree from its remote tip:

   ```bash
   git fetch origin <base>
   git worktree add <path> -b task/<short-description> "$(git rev-parse origin/<base>)"
   ```

   Never commit directly to either release train. If the ticket does not make
   the target clear, stop and ask.
4. Run the focused existing tests, type checks, and lint for the affected area. Stop
   on a relevant baseline failure. Record unrelated failures without widening scope.

## Run state

Keep a small state file inside the worktree at
`.projects/plans/<branch-name>.state.json`, beside the plan when there is one.
`.projects/` is git ignored, so it never reaches a commit.

```json
{
  "branch": "task/short-description",
  "ticket": "<id or null>",
  "path": "direct | quick | build",
  "stage": "setup | triage | plan | de risk | build | review | done check | finish",
  "status": "in progress | blocked | done",
  "open_findings": ["<review finding not yet fixed>"],
  "updated": "<ISO8601 UTC>"
}
```

Update it at each stage boundary, including `open_findings` as reviews produce
findings and fixes clear them. A run gets interrupted: a session ends, a context
fills, a day passes. Without this file the next run either repeats finished stages
or skips an unfinished one, and outstanding findings that lived only in a lost
context are lost with it. A direct path change finishes with nothing to resume, so
the file is optional there.

When a blocker or an unresolvable ambiguity appears after triage, set `status` to
blocked, record the open question, surface it, and stop. The resume check in Setup
picks the run up from there.

## Triage

Classify from the work, not from a requested process size.

| Path | Use when | Minimum proof |
| --- | --- | --- |
| Direct | Mechanical, non behavioral change | Focused validation and review of the diff |
| Quick | Small, single stream behavior change with clear criteria | Failing test, implementation, focused suite, code and scope review |
| Build | Multiple streams, architecture, security, deployment, or unclear interactions | Written plan, separate test and implementation contexts, reviews, and real surface verification when applicable |

Promote a direct or quick change when its real scope exceeds the selected path. Do
not add a planning artifact for a trivial mechanical change. If a quick change
turns up an assumption that qualifies for the de risk gate below, promote it to
build before its first edit. If that assumption only appears after an edit, stop
and either escalate or restart as a build path change; do not carry those edits
forward.

## Build rules

For a behavior change, write or update the test first and confirm it fails for the
intended reason. The test writer and implementer use separate fresh contexts when
the environment supports delegation. Otherwise keep the same separation in order:
write the test, run it red, then implement.

Keep edits within the ticket's acceptance criteria. Do not weaken tests, add
compatibility paths merely to preserve a caller, or replace a real integration with
an internal mock. Use mocks only for external or slow dependencies. Review every
caller when a return type or contract changes.

For a build path, write a short plan that identifies behavior sites, affected files,
test strategy, edge cases, and observable done conditions before implementation.
Have an independent available provider review the plan when the change is
architectural or crosses a contract boundary.

## Context discipline

Where the environment supports delegation, every brief tells its agent to return
under about three hundred words and to write anything longer to a file and return
the path. An agent's report stays in the caller's context and is re read on every
later turn, so a long report is paid for again on each one. This governs where
output goes and never what an agent checks or builds.

The same rule binds the caller. Send test suite output, diffs, reviews, and log
sweeps to a file and read back a count or a tail. Do not read a large artifact into
the caller's own context in order to check it.

## De risk gate

Default to no spike. Evaluate this gate only on the build path, after the plan and
any plan review, and before the first test is written. A direct or quick change
never runs a spike.

Run a spike only when all four of these hold:

1. The plan depends on a specific claim that has not been observed.
2. If that claim is false, the plan, scope, a dependency, or the architecture
   materially changes.
3. Documentation, source, existing tests, and prior observed evidence cannot settle
   the claim confidently.
4. A bounded experiment is materially cheaper than discovering the false claim
   during implementation.

If a failed assumption would not change an implementation decision, do not spike.
Treat the economics as a high bar, not a calculation. There should be a plausible
chance the assumption is wrong, failure should cost about a day of work or
invalidate a whole stream, the probe should fit inside thirty minutes, and the
avoided waste should clearly exceed the probe cost. Do not compute a numeric
probability.

Choose one of two kinds:

- Tracer, preferred whenever the experiment can safely be the first narrow
  production slice. It is kept production code and follows the normal test first,
  review, and verification rules. Run that slice alone: write its test, implement
  it, and harvest its observable result before releasing any remaining stream.
- Throwaway, allowed only when execution is required and the evidence cannot come
  from a safe production slice. Brief exactly one uncertainty, one observable pass
  or fail result, a scope limited to that question, a cap of thirty minutes, and a
  written finding. Run it outside the committed tree, never commit it, harvest the
  evidence, then delete it. Code existing is not a finding.

Run at most two throwaway spikes in one run without explicit user approval. If a
spike does not settle its question inside the cap, stop it and either halt or
escalate. Do not let it drift into implementation.

Every finding must confirm, revise, or reject the plan. When a finding materially
changes the plan, revise the plan and repeat whichever plan review applied to it. A
throwaway must settle before any test for the change is written. After a tracer, no
remaining stream proceeds until a revised plan clears that same review. When a
finding exposes unclear acceptance criteria or product behavior, stop and ask. A
spike gathers technical evidence and never makes a product decision.

## Review and verification

Every behavior changing change receives a code review and a scope review after the
affected tests pass. Reviewers are read only. Route findings back to the executor,
then rerun the relevant checks.

Each reviewer writes its full findings to
`.projects/plans/<branch-name>.findings.<reviewer>.md` and returns only a routing
index: how many findings, in which areas, touching which files. Fixes read the
detail back out of that file. A review is not complete until its findings file
exists, and that holds when the count is zero. A review whose only record is the
claim that it passed leaves nothing to check, and a skipped review looks exactly
the same from the outside.

Add a security review for authorization, secrets, payments, personally identifiable
information, or tenant boundaries. Verify user facing, deployment, runtime, chart,
or integration changes through the real affected surface. Static checks alone do not
prove a runtime acceptance criterion.

Before completion, run the full affected test suite plus the relevant type check and
lint. Check sibling paths when the change touches a known seam or guard. Demonstrate
that a new or modified guard rejects violating input through its real consumer path.

## Stop and escalate

Hand the decision back rather than pressing on when reviews cannot converge after
three rounds, when implementation reveals an approach the plan did not cover, when
real surface verification shows the shape is wrong, or when a choice needs
authority the ticket does not carry.

Stop for the same reason when the run itself stops converging: repeated fix and
review rounds without the review stage closing, an agent or a spawn count far past
what the change warrants, or a context that keeps hitting its limit. A run that
reports it is not converging is a successful outcome; a run that keeps spending is
not. Neither stop ever skips a review or weakens a gate.

## Finish

Confirm that the final diff satisfies every acceptance criterion and that validation
evidence reflects the final code. Present the diff and verification results. Open
the PR against the selected release train. For a general fix merged to `main`,
create or request the follow up PR that merges `main` forward into `next`; do not
normally cherry pick the fix. Follow the repository's commit, push, pull request,
and ticket status rules. Do not create a pull request or publish changes without
the authority required by those rules.
