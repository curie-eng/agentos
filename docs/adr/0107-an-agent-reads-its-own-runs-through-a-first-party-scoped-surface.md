# 107. An agent reads its own runs through a first-party surface, scoped by structured identity

Date: 2026-08-15

Status: Draft

## Context

An agent that cannot read its own run history cannot answer the questions the
channel actually asks it: "why did you say that an hour ago", "which tool call
failed on that turn", "what are you costing us per question". Issue
[#1060](https://github.com/curie-eng/curie/issues/1060) filed that gap from a
real bot built on nothing but the public surface. It is the rung above ADR-0100
(agents search their own surface through the channel port, Draft, in review):
0100 lets an agent read the *channel* it lives in, this one lets it read *what
it did* there.

The data already exists and ships by default. Langfuse is the single
observability backbone ([ADR-0004](0004-langfuse-observability-and-eval-backbone.md)),
the chart deploys it with bootstrap keys, and every turn is traced with prompt,
tool calls, timings, and token usage under a closed attribute schema
([ADR-0076](0076-closed-typed-telemetry-attribute-schema.md)). What is missing
is a path from that store to the model. Nothing in the sandbox can reach it:
the runner's boot environment is closed-world (ADR-0049), the platform API key
is deliberately kept out of the sandbox, and the two published community
Langfuse MCP servers are a one-star single-maintainer project and an unedited
`github.com/yourusername/...` template, neither of which is an acceptable
dependency for a component that can read every prompt the agent has ever
produced.

### Attribution today is a substring of a display name, and it has already failed

The important part of this decision is not transport. It is **whether a
returned run is actually this agent's run**. Today that is decided like this:

- the worker builds a session id as `f"agent-{agent_id}-thread-{thread_key}"`
  (`apps/worker/src/curie_worker/binding.py`);
- the runner names the trace `f"curie-run:{config.session.session_id}"`
  (`runner/src/curie_runner/__main__.py`);
- the API read proxy selects one agent's runs with a **client-side substring
  match** on that display name. `agent_trace_filter` returns `f"agent-{id}"`
  and `matching_traces` keeps traces whose `name` contains it
  (`apps/api/src/curie_api/metrics.py`, `apps/api/src/curie_api/langfuse.py`);
- that match runs over a **bounded newest-first scan of the whole project**,
  `_TRACE_SCAN_LIMIT = 500`, because Langfuse's list API has no substring
  filter.

The closed span-attribute enum (`runner/src/curie_runner/otel.py`) carries
`curie.session_id` and `curie.sandbox_id` but **no agent identity key**. There
is no structured field to filter on, so the only handle is prose.

That construction has already produced a wrong answer in front of a user. On a
cold-start v0.4.0-rc.4 stack, `/observability/metrics/summary` returned
`runs:13, tokens:2576, cost_usd:0.0506`, entirely seeded demo traces
(`eval:recl:1`, `metrics-cost-seed-...`), and the agent reading it, in its own
words, "nearly reported seed data as my agent's cost"
([#547](https://github.com/curie-eng/curie/issues/547)). The same class of
defect was observed again more recently: a bot asked about its own runs
answered from foreign telemetry. One Langfuse project holds every agent's
traces plus eval suites plus seeds (project-per-tenant is still unshipped, part
of [#155](https://github.com/curie-eng/curie/issues/155)), so a name-shaped
filter over a 500-row window is wrong in two directions at once. A busy install
pushes the agent's own runs off the tail, and everything else in the window is
one plausible name away from being claimed.

For an operator's dashboard that is a bug. For a self-observation surface it is
worse than absence: the agent states another workload's cost and tool calls as
its own, in a channel, in its own voice, with no signal that anything was
missed.

### Why this needs a decision rather than a connector

[#1063](https://github.com/curie-eng/curie/issues/1063) proposes declarative
connector hosting and notes in passing that it "resolves #1060 for free: an
agent reading its own Langfuse traces becomes just another connector."
Declarative hosting is the right mechanism for *arbitrary* MCP servers and this
ADR does not argue with it. But a connector's boundary is the credential it is
handed, and the credential here is the project key that reads **everything**,
so "just another connector" would ship the misattribution problem as a feature
and put a raw trace-store API in the model's context.
[#1118](https://github.com/curie-eng/curie/issues/1118) shows the mechanism is
also unfinished: derived `mcp_entries` are returned by the API and nothing
injects them into the sandbox yet, so a connector-shaped answer is blocked on
that lane regardless.

## Decision

**An agent reads its own runs through a first-party, read-only Curie surface,
auto-mounted like `curie-state`, scoped by a structured agent identity stamped
on the trace, and fail-closed on any run it cannot positively attribute.**

### 1. A `curie-runs` MCP server, auto-mounted, never bundle-shipped

The runner wires an in-process SDK MCP server into every real session, exactly
as it already does for approvals (ADR-0010) and the state store
([ADR-0073](0073-agentos-state-mcp-server-and-state-boot-env.md)). The verbs
are read-only and few: `list_runs` (window, capped count, newest first),
`get_run` (one run: turn input, tool calls and results, timings, usage,
approval decision), and `run_metrics` (aggregates over a window). No write verb
exists at any tier.

The backing is the read proxy that already exists, `apps/api`'s
`/langfuse/traces` and `/observability/metrics/*`
(`apps/api/src/curie_api/langfuse.py`, `apps/api/src/curie_api/routers/runs.py`,
`apps/api/src/curie_api/routers/observability.py`), not a second client and not
a new datastore. The agent-facing contract is Curie's, not Langfuse's, which is
what keeps ADR-0004's store swappable
([ADR-0007](0007-adopt-not-build-boundaries.md)). A connector pointed at
Langfuse's public API would make that vendor's response shape part of the agent
contract.

The feature is **off by default and enabled per agent by the operator**, on the
ADR-0098 grounds that what an agent may read is not a bundle-author knob.

### 2. Attribution is a structured attribute, and it lands first

The closed span-attribute schema gains an agent identity key (`curie.agent_id`,
alongside the thread key already embedded in `curie.session_id`), stamped by
the runner from boot-env facts it already receives. Selection filters on that
attribute server-side. **The name-substring filter is not the mechanism for
this surface**, and once the attribute exists the operator surfaces should stop
using it too.

Adding a key to a closed schema is a schema change under ADR-0076 and versions
under [ADR-0101](0101-schema-compatibility-for-closed-schemas.md), where a new
optional key is the minor case. It lands as its own change before anything
consumes it, the sequencing ADR-0098 used for its `BootEnv` field.

### 3. Fail closed, and say so

A run with no attribution attribute is **not returned**. It is not this agent's
until proven so. Two consequences follow directly and both are intended: traces
written before the key exists are invisible to the agent, and a partial or
truncated result set is reported as such (`"complete": false`, with the window
actually scanned) rather than presented as the whole history. An empty answer
is a correct answer; a confident wrong one is the defect this ADR exists to
prevent.

Cost follows the same rule for the same reason. Where Langfuse has no pricing
entry for the model (the OpenRouter case in #547), the surface reports cost as
**unknown**, never `0.0`.

### 4. Scope: the agent's own runs, bounded to the binding that asked

Two boundaries, both structural:

- **Never another agent's runs.** The scoped token
  ([ADR-0033](0033-scoped-sandbox-state-token.md)) is bound to this agent and
  the API refuses any other agent id server-side, so the fence holds against a
  bundle that skips the tool and calls the endpoint directly. That is the same
  two-sided argument ADR-0073 makes for its reserved namespaces.
- **Default to the requesting binding's scope.** Since multi-channel binding
  (#1525) one agent can serve several channels, and ADR-0100 already settled
  that the channel is the security line, so history from channel A must not
  surface in channel B. The default filter is therefore the agent *and* the
  bound scope of the turn asking. An operator may widen a specific agent to
  agent-wide reads; a bundle may not.

Per-run payloads are truncated at the surface with an explicit marker, so one
`get_run` cannot consume the model's context window.

## Alternatives rejected

**A first-party Langfuse connector via declarative hosting (#1063).** Rejected
as the *supported* surface, not as a mechanism. It hands the sandbox a
credential that reads every project's traces, makes a vendor API the agent
contract, and inherits exactly the attribution ambiguity that produced #547. It
is also blocked on #1118 today. Declarative hosting remains right for arbitrary
third-party MCP servers, and an operator who wants raw Langfuse access in a
bundle can still do that with eyes open.

**A bundle-shipped connector, or the community `langfuse-mcp` /
`mcp-langfuse`.** The status quo, rejected in #1060 and again here: every bot
rebuilds it, each copy independently gets credential handling, read-only
scoping, and truncation right or wrong, and the two published servers are not
dependencies to accept for a component that reads every prompt and tool result
the agent has produced.

**Open the reserved `transcript` state namespace to bundle code.** The
conversation-history port ([ADR-0029](0029-conversation-history-port-and-first-loader.md))
already persists transcripts, and ADR-0073 deliberately fences `memory` and
`transcript` from the bundle token. Rejected on both content and boundary. The
transcript is rehydration state for one thread and carries no tool-call detail,
timings, cost, or approval outcome, which are the exact fields the questions in
#1060 need; and unfencing a reserved namespace to add a read feature trades a
load-bearing guarantee for a surface that still would not answer the question.

**Point the agent at the observability CLI
([ADR-0038](0038-observability-cli-helper-for-the-agent-dev-loop.md)).** That
helper is an agent-dev-loop tool on the operator's box, and
[#866](https://github.com/curie-eng/curie/issues/866) records that it never
grew the query verbs: it only prints URLs. A sandboxed runner has neither the
binary nor the platform key.

## Consequences

**One new schema key gates the whole feature.** Nothing can be attributed until
the runner stamps agent identity, so the surface is inert until that change
ships and is emitting. That is the intended order, because a surface that
filters on prose is the thing being replaced.

**History has a start date.** Runs traced before the key exists are permanently
invisible to the agent. Backfilling from the display name would reintroduce the
substring match on the oldest and least verifiable data, so it is not done.

**What the agent can remember is bounded by trace retention**, which is an
operator policy the agent does not control. "I have no record of that turn" is
a real and expected answer, and the surface reports which window it scanned
rather than implying completeness.

**The read proxy's 500-trace client-side scan has to become a server-side
filter** for the agent path, or the surface silently truncates on any busy
install, which is the failure mode this ADR is written against. That work lands
with the attribute, not after it.

**An agent reading its own runs re-reads untrusted input.** Its prior turns
contain whatever the channel said, so the Slack AI exfiltration threat model
ADR-0100 records applies here too: retrieved content is data, it is truncated,
and it is never re-executed as instruction.

**The platform key still never enters the sandbox.** The surface adds no new
credential and no new datastore, just one scoped derivative under ADR-0033 over
the proxy that already exists, so the agent-proxy credential boundary
([ADR-0075](0075-the-agent-proxy-credential-and-egress-boundary.md)) is
unchanged.

**Operator surfaces get the same correctness for free.** The console Runs view,
`/observability/metrics/*`, and per-agent cost all read through the same
filter, so fixing attribution for the agent also fixes the dashboard that
misreported seed data in #547.
