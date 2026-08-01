# 91. Git-flow resolves deploy targets, so one repo serves many agents

Date: 2026-07-31

Status: Accepted

## Context

Issue #1070, and the last thing standing between
[ADR 0090](0090-a-reconciler-applies-connectors-so-agent-repos-need-no-cli.md)
and its own acceptance test.

ADR 0090 said an agent repository should contain only agent logic, and gave a
falsifiable criterion: sre-bot must be able to delete `.curie-version`, its
provisioning workflow, and both deploy workflows. A reconciler removes the need
for a CLI near the cluster. It does not remove the need for those workflows,
because git-flow cannot express what they express.

Git-flow resolves the agent for a push by repository, and there is exactly one:

```python
async def get_agent_by_repo(session, repo_full_name) -> Agent | None:
    ...select(Agent).where(Agent.repo_full_name == repo_full_name)
```

```python
op.create_index("ix_agents_repo_full_name", "agents", ["repo_full_name"], unique=True)
```

sre-bot has two agents on purpose — `sre-bot` on the prod channel, `sre-bot-dev`
on the dev channel — because that is what a dev/prod split of a Slack bot is.
Deleting its deploy workflows today would collapse them back into one, undoing
the separation [ADR 0089](0089-bundles-declare-their-deploy-targets.md) exists
to provide. So the repo keeps its workflows, keeps its version pin, and ADR
0090's criterion stays unmet no matter how good the reconciler is.

The information needed is already in the bundle. `deploy.yaml` names the agent,
the environment, and the channel per target, and the API can already resolve it
(`POST /deploy-targets/resolve`). Git-flow simply does not consult it: it maps
branch to environment and takes whatever single agent the repository row names.

## Decision

**Git-flow resolves a push through the bundle's `deploy.yaml`, and a repository
may bind more than one agent.**

A push to a branch selects the target whose name matches, then binds that
target's agent. `dev` selects the `dev` target, `main` selects `prod`. A branch
with no matching target is ignored exactly as an unmatched branch is today —
silently doing nothing is correct for a feature branch, and is the existing
behaviour.

`repo_full_name` stops being unique. It becomes what it always described: which
repository an agent is built from, with several agents legitimately sharing one.

**The trust model does not move, and this is the part to get right.** The
webhook's HMAC proves the sender holds the shared secret — the exact attacker
`apps/api/CLAUDE.md` assumes — so `clone_and_archive` never takes the payload's
clone URL. It derives `trusted_clone_url` from `Settings.github_clone_base` plus
the `repo_full_name` **read from the database row**, and compares the payload's
URL against that to reject a forged push (`CloneOriginMismatch`).

With several agents per repository that derivation must not become ambiguous.
It does not: every agent bound to a repository has the same `repo_full_name`, so
the derived origin is identical whichever row is read. The clone is authorized
by the repository binding, and the target only decides which agent receives the
resulting Version. Those are separate questions and the change keeps them
separate.

**Bundle-once, bind-many.** A push builds the bundle once and creates a Version
once; the target decides which agent's Deployment points at it. This preserves
the rule `apps/api/CLAUDE.md` states — prod promotes the exact artifact dev
validated — and extends it: two agents from one push are the same bytes by
construction, not by discipline.

## Consequences

sre-bot can delete `.curie-version`, `provision-curie.yml`, `deploy.yml`, and
`deploy-dev.yml` — roughly 400 lines, none of which is about answering SRE
questions. That deletion is ADR 0090's acceptance test and this is what makes it
reachable.

An agent's identity moves from "the repo it came from" to "the target that names
it". That is a real conceptual change. `get_agent_by_repo` becomes
`get_agents_by_repo`, and every caller must say which agent it means rather than
receiving the only one.

Dropping a unique index is a one-way schema change in practice: re-adding it
later requires resolving whatever duplicates exist by then. This is worth doing
once, deliberately, rather than discovering later that a second agent was quietly
impossible.

A `deploy.yaml` that names an agent bound to a *different* repository is now
expressible, and would let one repository's push deploy over another's agent.
That is a new authorization question this ADR does not answer by itself: the
implementation must reject a target whose agent is bound elsewhere. It is the
sharpest edge here and should be the first test written, not the last.

Two paths will resolve targets — the CLI's `--target` and git-flow's branch
match. They must agree, so both go through the same resolution the API already
exposes rather than growing a second copy. A repository whose `deploy.yaml`
resolves differently depending on who asked would be worse than either path
alone.

## Alternatives considered

- **Keep one agent per repo; use one repo per agent.** Rejected: it doubles
  every bundle, and the two copies drift. It also breaks promote-what-you-
  validated outright, since prod's artifact is then a different repository's
  build rather than the same bytes.
- **Let the deploy workflows keep doing this.** Rejected: it is the status quo,
  and it is what ADR 0090's acceptance criterion exists to eliminate. A repo
  that must carry deploy plumbing to have two environments has not been freed of
  platform concerns.
- **Encode the agent in the branch name** (`deploy/sre-bot-dev`). Rejected: it
  puts routing in a branch-naming convention, which is neither reviewable nor
  validated — the same objection ADR 0089 made to routing living in CI.
- **A per-repo mapping held by the platform** rather than in `deploy.yaml`.
  Rejected for ADR 0086's reason: state the repository cannot see means a
  rebuilt cluster does not reproduce from the repo, and the change is invisible
  in review.
