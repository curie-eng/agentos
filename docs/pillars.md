# Agent-development pillars

Curie's issues, pull requests, and discussions are organized around eight
recurring concerns in agent development, not around the code interfaces or
components a given change happens to touch. The pillars group work by the
problem it solves, so a maintainer or contributor can filter to everything
about, say, model routing or security policy, regardless of which package or
service the change lives in. The taxonomy was proposed and agreed in
[discussion #1176](https://github.com/curie-eng/curie/discussions/1176).

## The eight pillars

Each pillar has a corresponding `pillar:*` label.

1. **Capability authoring** (`pillar:capability-authoring`): prompts, tool
   integration, and subagent decomposition. What an agent can do, and how a
   contributor builds or tunes that capability.
2. **Model and routing strategy** (`pillar:model-routing-strategy`): choosing
   and routing across LLMs for different tasks, and the credentials that go
   with a given model or provider.
3. **Memory and institutional knowledge**
   (`pillar:memory-institutional-knowledge`): durable, persistent memory
   across agent hierarchies.
4. **Connectivity and channels** (`pillar:connectivity-channels`): how agents
   communicate with people and with other agents, across Slack, Teams, email,
   GitHub, and any other channel humans already use.
5. **Security and governance** (`pillar:security-governance`): what actions an
   agent may take on a tool, on behalf of a user or as a service identity; who
   may invoke an agent and who it may reply to; human-in-the-loop controls and
   killswitches; and resistance to adversarial input.
6. **Observability, debugging, and auditability**
   (`pillar:observability-auditability`): local and production tracing and
   diagnostics, plus the tamper-proof record of what an agent did and why.
7. **Evaluation and continuous improvement**
   (`pillar:evaluation-continuous-improvement`): pre-deploy testing and
   verification, and the production feedback loop that improves an agent
   after it ships.
8. **Deployment and lifecycle management** (`pillar:deployment-lifecycle`):
   versioning, promotion, rollback, and the rest of an agent's release
   lifecycle.

## Applying the labels

- **Issues and epics**: always label with the pillar (or pillars) the work
  belongs to. This is the primary place the taxonomy earns its value, for
  triage and for filtering a backlog by concern.
- **Pull requests**: a PR that closes a labeled issue inherits that issue's
  pillar and does not need its own label. Label a PR directly only when it has
  no linked issue, such as a drive-by fix, or when it genuinely spans more
  than one pillar.
- **Discussions**: an unscoped idea in the `Ideas` category should carry a
  pillar label when it clearly falls under one. Discussions that are general
  questions or support requests (the `General` and `Q&A` categories) do not
  need one.

## Scope and change process

These eight pillars are not guaranteed to be exhaustive forever; new concerns
may surface as the project grows. Propose a new pillar, or a change to an
existing one, as a GitHub discussion under `Ideas` before creating the label,
the same way this taxonomy itself was proposed.
