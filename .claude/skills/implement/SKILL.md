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
3. Check for an existing branch or worktree for the ticket. Otherwise create a fresh
   branch and worktree from the repository's current target base, following its own
   instructions.
4. Run the focused existing tests, type checks, and lint for the affected area. Stop
   on a relevant baseline failure. Record unrelated failures without widening scope.

## Triage

Classify from the work, not from a requested process size.

| Path | Use when | Minimum proof |
| --- | --- | --- |
| Direct | Mechanical, non behavioral change | Focused validation and review of the diff |
| Quick | Small, single stream behavior change with clear criteria | Failing test, implementation, focused suite, code and scope review |
| Build | Multiple streams, architecture, security, deployment, or unclear interactions | Written plan, separate test and implementation contexts, reviews, and real surface verification when applicable |

Promote a direct or quick change when its real scope exceeds the selected path. Do
not add a planning artifact for a trivial mechanical change.

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

## Review and verification

Every behavior changing change receives a code review and a scope review after the
affected tests pass. Reviewers are read only. Route findings back to the executor,
then rerun the relevant checks.

Add a security review for authorization, secrets, payments, personally identifiable
information, or tenant boundaries. Verify user facing, deployment, runtime, chart,
or integration changes through the real affected surface. Static checks alone do not
prove a runtime acceptance criterion.

Before completion, run the full affected test suite plus the relevant type check and
lint. Check sibling paths when the change touches a known seam or guard. Demonstrate
that a new or modified guard rejects violating input through its real consumer path.

## Finish

Confirm that the final diff satisfies every acceptance criterion and that validation
evidence reflects the final code. Present the diff and verification results. Follow
the repository's commit, push, pull request, and ticket status rules. Do not create
a pull request or publish changes without the authority required by those rules.
