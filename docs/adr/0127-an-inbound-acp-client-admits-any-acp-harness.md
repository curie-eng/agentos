# 127. An inbound ACP client admits any ACP harness behind the harness port

Date: 2026-08-23

Status: Draft

**Amends [ADR-0040](0040-adopt-acp-as-an-edge-projection.md)** by adding one
decision, the inbound direction. ADR-0040's decision 1 (ACP is an edge
projection, never the internal canonical type) is preserved verbatim and is
extended to bind in both directions. Its decisions 2, 3, 4, 5 and 6 stand
unchanged; this ADR adds a client path beside the server path of decision 2, it
does not replace it. On acceptance, the back link ADR-0045 requires is added to
ADR-0040's header; a Draft amends nothing yet, so no edit to ADR-0040 is made
here.

Tracked by [#1609](https://github.com/curie-eng/curie/issues/1609). Per
[ADR-0085](0085-acceptance-not-implementation-authorizes-an-adr.md) as amended
by [ADR-0102](0102-accepted-alongside-implementation-with-explicit-approval.md),
this Draft authorizes no implementation. No ACP client, harness package, or
runner entry point is written until it is Accepted with explicit maintainer
approval. This ADR builds on
[ADR-0119](0119-a-resumed-thread-rebuilds-its-prefix-so-the-prompt-cache-still-hits.md)'s
full, prefix-preserving structured message replay. Its implementation is
blocked by that ADR's top-of-backlog implementation ticket
[#1902](https://github.com/curie-eng/curie/issues/1902): durable ACP
suspend/replay cannot be implemented until #1902 lands.

## Context

ADR-0040 adopted ACP and aimed it **outward**: `curie runner acp` speaks ACP over
stdio so Zed, the JetBrains IDEs, Neovim and Emacs can embed Curie. That is the
editor-embedding problem, and it is the only direction ADR-0040 decided.

The reverse direction is a different and currently more expensive problem. Adding
a second harness today means writing an ACI server per vendor.
`docs/architecture-vision.md` states the cost honestly: a candidate harness must
implement an HTTP process serving the ACI wire, and "the harness port is
entangled with the plugin format", because `packages/plugin-format` is the Claude
Code plugin shape verbatim. ADR-0060 withdrew OpenCode-as-second-harness after
its steer spike *succeeded*, precisely because of that unpriced per-vendor tail:
the synthesis tax of forging another harness's dataclasses, plus the installer
and the bundle compiler. One harness has ever crossed this port.

Meanwhile the same fifty-plus ACP agent registry ADR-0040 already cites —
Claude Code and Codex among them, with OpenCode shipping native ACP — is a set
of engines that already speak a normalized turn and permission wire. Curie
reimplements per vendor what those engines already export once.

**Prior art, and the precise shape of the borrow.** Vercel's AI SDK harness layer
ships a `createACP()` meta-adapter: one integration written against ACP admits
any ACP-compatible harness, rather than one integration per vendor
([changelog](https://vercel.com/changelog/use-acp-compatible-harnesses-with-the-ai-sdk-harness-layer)).
Both that work and ACP itself are Apache-2.0. What is borrowed is the **direction
of the adapter** — inbound, one adapter parameterized by which ACP agent it
launches — and nothing else. No TypeScript is copied, and the surrounding AI SDK
model is not adopted. Block's Buzz, xAI's Grok Build, and Agentrove independently
converged on ACP as the interop layer in the same window, which is the signal
that this is an ecosystem standard rather than one vendor's convenience.

Two constraints from the existing corpus bound the answer. ADR-0060 makes a
harness a declared package registering a `HarnessContribution`
(`runner/src/curie_runner/harness/contribution.py::HarnessContribution`) through
a guarded entry point. ADR-0061 is a **Draft**, gated on a spike, proposing that
the harness boundary become an out-of-process `create_app() -> FastAPI` adapter;
until that spike answers, the port in the tree is the in-process `ModelSession`
Protocol (`runner/src/curie_runner/adapter.py::ModelSession`), whose values are
mapped by `translate_message`
(`runner/src/curie_runner/translate.py::translate_message`) into the ACI outbound
union.

## Decision

**1. Add one inbound ACP client adapter behind the harness port.** Curie may act
as an ACP **client**, driving an ACP-speaking agent as its engine, in addition to
acting as an ACP server for editors. The adapter launches the configured ACP
agent, performs `initialize`, opens a session, sends prompts, and consumes
`SessionUpdate` notifications, mapping them into whatever internal turn
representation the harness port carries.

**2. ADR-0040's decision 1 binds inbound as well as outbound: ACP is never the
internal canonical type.** Outbound, Curie projects its internal turn
representation into `acp::SessionUpdate`. Inbound, Curie maps `SessionUpdate`
*into* that internal representation at the adapter edge. In both directions ACP
is a wire, containment to the edge is the property being bought, and no consumer
above the port ever sees an ACP type. Which internal type that is remains
deliberately open for exactly the reason ADR-0040 gave: ADR-0061 is a Draft gated
on a spike, and naming a source type now would freeze a premise the spike has not
answered.

**3. One adapter, parameterized by which ACP agent it launches — not one package
per vendor.** The ACP client registers a single `HarnessContribution` under
ADR-0060. An individual ACP agent (Claude Code, Codex, OpenCode, any of the
registry) is **configuration of that contribution** — the command to launch, its
auth shape, its declared read-only tool set — not a separate registry key. This
is the economic claim for the normalized turn wire and read-only tool surface:
admitting the N+1st ACP agent there is a configuration entry, not a new ACI
server. Writable tools have the additional trusted-identity requirement in
decision 4 and remain unavailable for a peer that does not supply it. The
single contribution also keeps ADR-0060's fail-closed guard rules meaningful,
because one contribution claiming one key cannot silently shadow a built-in by
fanning out into fifty.

**4. The approval reverse-request enters Curie's durable suspend/replay
lifecycle, never an adapter-local policy.** An ACP agent driven as an engine
will issue `session/request_permission` back at Curie. Stable ACP v1 does not
make that request an authorization identity: its `ToolCallUpdate` requires only
`toolCallId`; the title, kind, and raw input are optional display fields and may
default on decode error
([ACP v1 schema](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/schema/v1/schema.json)).
The generic adapter therefore never derives grant authority from those fields.

Writable permission support requires a peer-specific **permission identity
contribution** at a Curie-controlled execution boundary, such as the wrapper or
MCP proxy that actually gates the call. It maps the peer call to one exact entry
in the validated bundle tool registry and canonicalizes the arguments before
execution. An absent, unknown, ambiguous, or undeclared mapping fails loud and
the peer remains read-only. The runner authors the durable permission-gate
provenance, tool name, and canonical input digest from that trusted contribution,
under ADR-0046 and [#1956](https://github.com/curie-eng/curie/issues/1956); it
never copies ACP display data into `approval_granted_tool` or the digest. The
writable path cannot ship until #1956 carries that digest through its own
reviewed, backward-compatible ACI change.

The approval card and durable human-readable summary are rendered from that
same trusted canonical tool identity and input, or from an immutable rendering
bound to their digest. ACP `title`, `kind`, and `rawInput` may be retained only
as explicitly untrusted diagnostics; they never supply or override the text a
human approves. The operation that later matches the digest is therefore the
operation the approval surface described, not merely an operation hidden behind
benign peer-authored display fields.

The adapter invokes that existing callback; the worker and API persist the
durable `Approval`. The adapter itself writes no durable state. Once the block
is recorded, it responds to the pending reverse-request with
`RequestPermissionOutcome::Cancelled`, issues `session/cancel`, waits a bounded
interval for the prompt to stop, then terminates the process and releases the
sandbox. No allow outcome reaches the original invocation. This ordering is the
ACP cancellation contract, and it prevents the denied attempt from executing
while avoiding an ACP JSON-RPC request held open for human-shaped time.

After approval, Curie starts a fresh turn with an ADR-0035 one-shot grant bound
to the trusted tool identity, canonical input digest, approval id, and absolute
approval expiry. For this adapter, one ACP `session/prompt` cycle is one Curie
turn for grant lifetime: the grant may survive preceding read-only work in that
cycle, but the trusted execution boundary atomically rechecks the digest and
expiry before consuming it. A mismatch or expiry denies without consuming the
grant. The grant also expires when the prompt cycle returns, so a peer cannot
carry it into a later prompt; the absolute expiry prevents a peer from retaining
authority by holding one prompt open indefinitely.

This inbound path is deliberately stronger than ADR-0035's accepted
tool-name-only baseline because the reissuing principal is a different external
process. Here "exactly once" means the denied pre-approval attempt executes zero
times and exactly one post-approval call with the approved canonical operation
may pass. The ACP client holds no local policy, allowlist authority, or default
answer. ADR-0040's decision 4 is unchanged and is restated here in the inbound
direction: **an ACP peer's opinion is input, never authority**, in whichever
direction the request travels.

**5. Session persistence uses ADR-0119 structured replay; ACP-native resume is
optional.** ADR-0040's decision 5 stands: no file-per-session JSONL, and the
adapter owns no durable state of its own. It writes through the existing
approval and conversation planes named in decision 4. Fresh ACP processes
rehydrate from ADR-0119's full, prefix-preserving structured message replay —
not the current text preamble path — so the resumed turn has the durable context
needed to reissue an approved operation. An ACP-native resume identifier may be
used as an engine-specific optimization where available, but is not required
and is not Curie's portable session contract. ADR-0040's decision 6 stands:
negotiate the protocol version at `initialize`, accept the compatible range,
and fail loud naming both versions on an incompatible peer. An inbound
permission request is control-bearing in the ADR-0036 sense, so an unmodelable
token is a loud error, never a silent degrade.

**6. Conformance is the gate, and conformance is not elevation.** The ACP client
is conformant only when it passes ADR-0062's extended suite — the ACI checks
(`packages/aci-protocol/src/aci_protocol/conformance.py::run_conformance`,
exercised by `runner/tests/test_conformance.py`) plus the read-only tool set,
credential resolution, bundle compile, and telemetry seams. Its permission
conformance case must prove, with a real ACP agent: permission request, trusted
tool and input resolution, `Cancelled` response, process end, approval, fresh
process, ADR-0119 rehydration, zero execution before approval, and exactly one
matching execution during the resumed prompt cycle. Independent controls must
prove that changed arguments, an absent identity contribution, rejection,
absolute expiry while the prompt remains open, and reissue on a later prompt all
execute zero times. A conflicting peer title/raw input must not change the
approval card or durable summary rendered from the trusted canonical operation.
Passing makes an ACP agent *wireable*, not *shippable*: per ADR-0062 decision 4,
parity evals under ADR-0022 remain the bar before any ACP-driven harness is
elevated past spike status.

**7. Explicit non-goals.** This ADR does not replace the ACP server path of
ADR-0040 decision 2, which continues to serve editor embedding. It does not
adopt ACP's remote transports, which remain upstream work in progress. It does
not dissolve the plugin-format entanglement: an ACP agent still needs a
`compile_bundle` hook that turns a validated Curie bundle into whatever
configuration that engine accepts, and ACP says nothing about that. And it does
not fork or vendor any third-party adapter.

## Alternatives considered

1. **Keep writing an ACI server per vendor (the status quo).** Rejected. It is
   the exact cost ADR-0060 recorded when it withdrew OpenCode after a *successful*
   spike, and one harness has crossed the port since. The per-vendor tail, not
   the protocol translation, is what kills a second harness.
2. **Make ACP the internal canonical type in both directions.** Rejected for the
   reason ADR-0040 already rejected it outbound: it discards the cross-harness
   normalization the internal type exists for and couples the core's message
   model to an external crate's enum evolution. Inbound it is worse, because the
   normalization job is larger on the side where heterogeneous engines actually
   arrive.
3. **Register one harness package per ACP agent.** Rejected. It restores the
   per-vendor cost this ADR exists to remove, multiplies ADR-0060 registry keys
   without adding information, and makes the guard rules police fifty near-identical
   entries instead of one.
4. **Port Vercel's `createACP()` implementation.** Rejected. The license permits
   it, but the artifact is TypeScript against the AI SDK's own harness layer and
   Curie's port is Python behind a different seam. The transferable asset is the
   adapter's direction, which costs nothing to adopt and carries no dependency.
5. **Wait for ADR-0061's spike to settle the boundary first.** Rejected as a
   blocker, accepted as a sequencing note. This decision is about *which
   direction* the ACP relationship runs, which is orthogonal to whether the port
   is an in-process Protocol or an out-of-process app; decision 2 is written to
   survive either outcome. Implementation still waits on a port to plug into, the
   same way ADR-0040's projector waits on a seam to project from.
6. **Keep the ACP JSON-RPC request or process alive while a person decides.**
   Rejected. Human approval time is unbounded compared with a turn, retains a
   sandbox and process that must be releasable, and cannot survive their failure.
   Decision 4 instead cancels safely and replays after durable approval.
7. **Use the current text conversation preamble as replay.** Rejected. It
   cannot reconstruct the structured prefix and exact turn context needed for a
   portable post-approval reissue; ADR-0119 and #1902 establish that prerequisite.
8. **Do nothing.** Rejected. Curie stays a one-harness platform whose second
   harness is permanently priced at an ACI server plus an installer plus a bundle
   compiler, while the engines it wants already export a normalized wire.

## Consequences

- A second and third harness become a configuration entry plus a conformance run
  for the turn wire and read-only surface, instead of a per-vendor server. A
  writable peer still needs the trusted permission identity contribution in
  decision 4; ACP v1 alone cannot supply it.
- The runner gains an ACP client dependency at the harness edge, next to the ACP
  server dependency ADR-0040 already accepted. Version skew is handled by
  decision 5; containment to the edge is what decision 2 buys.
- Curie speaks ACP in both directions, which means the mapping is written twice
  and the two must not drift. The mitigation is that both sides map against the
  same internal turn representation, so the internal type stays the single point
  of truth exactly as ADR-0040 decision 1 intends.
- The honest tradeoff, stated plainly: **an inbound ACP agent's tool call is
  gated by a plane it does not know exists.** ADR-0040 already recorded this cost
  outbound, where a client's UX assumes local authority and gets a round trip
  instead. Inbound it lands on the engine: an ACP agent expecting a fast local
  permission answer will be cancelled and later rehydrated to reissue the
  operation after Curie's durable approval latency, or receive no grant on
  rejection or expiry. Decision 4 is not negotiable, so this cost is accepted
  rather than designed away.
- The adapter remains durably stateless and does not reserve a sandbox during an
  approval wait. Its writable permission path is consequently unavailable until
  #1902 implements ADR-0119's structured replay and #1956 lands the separately
  reviewed argument-bound grant contract. A read-only spike may precede those
  prerequisites; no writable ACP client implementation begins from this Draft.
- ACP normalizes the turn and permission wire. It does **not** normalize the
  installer, the credential shapes, or the bundle format. Anyone reading this ADR
  as "a second harness is now free" is reading it wrong; the claim is that the
  most expensive third of that bill is removed, and decision 7 names what remains.
- The ordinary approval lifecycle stays in the worker and API, but #1956 must
  extend its durable provenance with a canonical input digest before the
  writable path can ship. This ADR does not modify the frozen contract itself.
