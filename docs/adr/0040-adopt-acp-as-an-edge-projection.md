# 40. Adopt the Agent Client Protocol as an edge projection

Date: 2026-07-16

Status: Draft

## Context

The Agent Client Protocol (ACP) is an open standard created by Zed Industries,
released August 2025 under Apache-2.0, speaking JSON-RPC 2.0 over stdio (an
HTTP/WebSocket transport for remote agents is upstream work in progress). Its
pitch is "LSP for coding agents": decouple any editor from any agent, reusing
MCP's JSON representations where they fit and adding the agentic-UX types MCP
lacks (diffs, permission requests, session updates). Spec at
agentclientprotocol.com; reference implementation at
github.com/agentclientprotocol/agent-client-protocol.

Adoption is far enough along to matter to us. The ACP agent registry lists
roughly fifty agents, Claude Code and Codex among them, and OpenCode ships
native ACP support. On the client side Zed and the JetBrains IDEs are native,
with Neovim and Emacs carried by the community. The decisive fact is that
**both** harnesses Curie wraps (ADR-0005's claude-agent-sdk adapter, ADR-0011's
OpenCode second harness) are already ACP-aware, so this is not swimming upstream.

Curie has no editor-embedding story at all. Its clients are the CLI, the Slack
dispatcher, and the UI, and each new client surface re-implements turn rendering
and approval prompting from scratch. ADR-0020 pushed rendering out of the channel
interface, but it did not give the outside world a contract to render against.

Two existing decisions bound the shape of any answer. ADR-0031 decided that **the
runner owns its message model**: the SDK dataclasses leaving `ModelSession.receive_turn`
become a runner-owned `TurnEvent` union (`AssistantText | ToolCall | RateLimit |
TurnResult`), with the Claude adapter mapping SDK to TurnEvent and other harnesses
emitting TurnEvent directly. That union exists for one purpose ADR-0031 states
plainly: it is the one extraction that shrinks per-harness work, because it
normalizes across heterogeneous harnesses. ADR-0034 and ADR-0035 fix the approval
model: a gated tool call is denied at the runner's `can_use_tool`, the turn ends
`awaiting-approval`, the worker persists a durable `Approval` and suspends, the
API resolves who may approve **server-side** behind a single authorizer over four
approver sets, and a resume turn carries a one-shot allowance so the approved call
can complete exactly once.

Prior art was reviewed. xAI's Grok Build uses the ACP `SessionUpdate` enum as its
single canonical turn type across TUI, headless, and IDE surfaces, and its ACP
`request_permission` reverse-request is precisely the human-in-the-loop shape
Curie already implements durably. That is a real signal about the protocol's
fitness, and also a warning about how much of Grok's approach transfers.

## Decision

**1. Adopt ACP as an edge projection, not as the internal canonical type.** Add a
projector at the runner edge mapping the runner's internal turn representation to
`acp::SessionUpdate`. The internal type stays internal because it does a job ACP
does not do: normalize across heterogeneous harnesses. Grok can make "ACP
`SessionUpdate` is the one true type" work only because it is a single
self-contained harness with nothing to normalize. Curie is not that, and
collapsing the internal type into the wire type would put the cross-harness
normalization back in every consumer.

**Which internal type the projector reads is deliberately left open.** This
decision originally named ADR-0031's runner-owned `TurnEvent` union. ADR-0031 is
now Superseded by [ADR-0060](0060-the-harness-is-a-declared-package.md) and
[ADR-0061](0061-out-of-process-harness-boundary.md), and under ADR-0061 the
harness boundary is a process rather than a Protocol, with `TurnEvent` demoted to
that ADR's recorded fallback if its spike fails. Naming a source type now would
freeze a premise the spike has not answered. This decision binds the *shape* --
edge projection, never replacement -- and the source type follows whatever the
harness boundary settles on.

**2. Ship an ACP server entry point (stdio first).** A dedicated `curie runner acp`
subcommand speaks ACP over stdio and projects turns through decision 1. Remote
transports stay out of scope until upstream stabilizes them. This buys embedding in
Zed, the JetBrains IDEs, Neovim, and Emacs against an open standard rather than four
bespoke integrations.

**3. Project the gated-tool approval as ACP `request_permission`.** Curie already
owns the hard half: the durable `Approval`, the suspend, and the reconciler-driven
resume (ADR-0034, ADR-0035). ACP supplies the wire contract for the easy half, so any
ACP client can render an approval and carry a human's answer back without per-client
work.

**4. The authorizer remains the sole authority; ACP is a rendering and transport
contract, not a trust boundary.** A client answering `request_permission` is not
authority, it is input. Membership resolution stays server-side in the API per
ADR-0034, and the one-shot allowance semantics of ADR-0035 are unchanged: the answer
travels to the API, the authorizer decides, and only then does a resume turn carry a
grant. Any design in which an ACP client's answer is trusted directly by the runner
is explicitly rejected, because it re-creates the caller-asserted-actor defect
ADR-0033 and ADR-0034 exist to close.

**5. Do not adopt ACP's or Grok's session-persistence model.** File-per-session JSONL
is a single-user local-workstation assumption and is wrong for Curie's concurrent
multi-tenant reality (ADR-0008, ADR-0013). Durable state stays exactly as it is; the
projector is stateless.

**6. Pin and negotiate the ACP protocol version, with a reader policy mirroring
ADR-0036.** ADR-0036 settled this argument once for the ACI: a version number on top
of readers that cannot express a compatibility range is decoration, and unknown
control-bearing tokens reject rather than degrade. The ACP edge inherits that posture.
Negotiate the version at initialize, accept the compatible range, and **fail loud with
both versions named** on an incompatible peer rather than silently degrading. An ACP
permission request is control-bearing in the ADR-0036 sense, so a token we cannot
model is a loud error, not a fallback.

## Alternatives considered

- **Replace the internal type with `acp::SessionUpdate` outright (Grok's approach).**
  Rejected. It discards the cross-harness normalization the internal type exists
  for, and it couples the core's message model to an external crate's
  enum evolution, so every upstream ACP release becomes a core refactor.
- **Bespoke per-editor integrations.** Rejected: N integrations, no standard, and each
  one re-implements turn rendering and approval prompting, which is the cost this ADR
  is trying to stop paying.
- **Do nothing.** No editor embedding at all, and every new client surface keeps
  re-implementing turn rendering and approval prompting. Viable only if editor
  embedding is never a goal.

## Consequences

- IDE embedding and an open standard for roughly the cost of one projector plus one
  entry point. Nothing in the worker, the API, or the approval plane moves.
- The projector is a pure function of the internal turn type, which makes it cheap
  to test and keeps the conformance safety net intact: `run_conformance`
  (`packages/aci-protocol/src/aci_protocol/conformance.py`, exercised by
  `runner/tests/test_conformance.py`) must stay green for the Claude, fake, and
  OpenCode sessions with the projector in the tree.
- ACP's remote transport is upstream work in progress, so stdio-only initially
  constrains the embedding story to local subprocess clients. Remote embedding waits
  on upstream.
- A new external dependency (the `agent-client-protocol` crate) enters the runner
  edge. Version skew is handled by decision 6, and containment to the edge is what
  decision 1 buys.
- The honest tradeoff: **ACP's permission model is client-mediated by design**, which
  is a weaker posture than Curie's API-side authorizer. Decision 4 is what
  reconciles them, and it has a visible cost: Curie will not implement
  `request_permission` the way the spec naively reads. The client renders and relays;
  it does not decide. Clients whose UX assumes local authority over the answer will
  see a round trip and, sometimes, a denial.
- The internal turn type this projects from is not settled: ADR-0031 was superseded
  by ADR-0060 and ADR-0061, and ADR-0061 is a Draft gated on a spike. This ADR is a
  decision about where ACP sits relative to that seam, and the projector cannot be
  built before the seam it projects from exists.
