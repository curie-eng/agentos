---
seam: Third-party port adapter (deployed service)
kind: NONE
impls: 0 (intended line recorded, nothing built)
grade: not separately graded
epics:
  - "#19"
  - "#158"
order: 22
---
# INTERFACE: Third-party port adapter (deployed service)

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** NONE &nbsp;·&nbsp; **Implementations today:** 0 (intended line recorded, nothing built) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Every other file in this catalog answers "where does the code already draw a
line for *this* seam". This one answers the question that sits above all of
them, and that ADR-0096 settles: **where does a third party put its code so the
platform uses it instead of the default?**

The answer is a **deployed service**, not a loaded plugin. A third-party
platform adapter speaks the port's versioned wire contract and is selected by
composition — chart values, compose, or endpoint config — rather than by runtime
code loading. Four reasons, in the ADR's order of weight: it is language-neutral,
so an internal-tools team writes its adapter in the stack its tool already lives
in rather than being conscripted into Python; its blast radius is a process, so a
crash or a hang in third-party code is a failed dependency rather than a wedged
worker; it is independently upgradable behind a versioned wire; and it is what
the system already does everywhere a second implementation exists, so it adds no
new operational concept.

The line this file records is therefore a **placement constraint**, and nothing
implements it yet. That is what NONE is for.

## Current contract

There is no contract to conform to yet. What a first implementation must honor,
taken from ADR-0096 rather than invented here:

- **Two plugin kinds that do not merge.** An *agent bundle* extends one agent, is
  the Claude plugin shape verbatim, runs inside the sandbox trust domain, and
  versions with the agent. A *platform adapter* implements a platform port, runs
  in the platform trust domain, holds platform-adjacent credentials, and versions
  with the deployment. A request to plug in a new channel, store or harness is
  never answered by the bundle format. The rule is narrow and sharp: a bundle's
  files never execute as platform code, while declaring an implementation for the
  platform to host is allowed and already shipped — see
  [connector-host](../connector-host/INTERFACE.md), which is that same
  declare-and-host split one scope down, agent-scoped instead of
  deployment-scoped.
- **The wire is HTTP or a queue payload.** A bespoke RPC plugin protocol was
  rejected because the platform already composes over contracts that are
  versioned, drift-gated and understood by every lane; revisit only if a port's
  contract genuinely cannot be expressed that way, which none currently is.
- **A four-rung promotion ladder before a port is pluggable.** An `INTERFACE.md`
  documents where the code already draws the line; a contract package makes the
  line a schema, as `packages/aci-protocol` and `packages/channel-protocol`
  already do; a conformance suite is something a third party runs against its own
  adapter before it ever talks to us; and the port owes an entry in an adapter
  manifest schema naming the config, secrets and endpoints an implementation
  declares. Without that fourth rung a third party can be conformant and still
  not installable, which counts as an unfinished promotion rather than a finished
  one. A registry proves an object was loaded; a conformance kit proves it
  behaves.
- **Credential rules, which are the trust boundary.** An adapter is a rendering
  and transport contract, never a trust boundary: approvals resolve solely
  through the API authorizer, and an adapter's report of who clicked is input
  rather than authority. An adapter holds its own channel credentials and never
  the platform's model credentials, and **a reply endpoint never receives any
  credential other than its own**, so per-endpoint authentication is part of the
  egress contract a promoted port publishes rather than something bolted on later.
- **Ingress is the authenticated hook surface, never the broker.** A third-party
  adapter enqueues nothing directly: raw produce access can mint a turn for any
  agent and forge its author, bypassing the dedupe and authentication API ingress
  exists to provide. Direct enqueue stays first-party only.
- **In-process entry points are the narrow exception.** Reserved for ports that
  are latency- or transaction-coupled to a turn and would be absurd behind HTTP —
  the binding hook that runs at claim time before the sandbox boots
  (`apps/worker/src/curie_worker/binding.py`), approver-set membership on the
  approval path (`apps/api/src/curie_api/approvers.py::ApproverSet`), and eval
  scorers inside a graded sweep. Those would use per-port `curie.<port>`
  entry-point groups carrying the same fail-closed guard rules as the harness
  registry, described in [harness-package](../harness-package/INTERFACE.md). The
  cost is why it stays the exception: the contribution must be pip-installed into
  a service image, which means a derived image per service, rebuilt on every
  platform release.
- **Ports are promoted one at a time, on demand, and the channel port is
  first** — the only seam with named third-party askers and the only one graded
  `C`. No generic plugin framework is built now, because most seams have one
  implementation and no asking party, and a framework would encode guesses about
  second implementations that do not exist. This is the standing restraint of
  [architecture-vision.md](../../architecture-vision.md) applied to the plugin
  question itself.

## Implementations today

None, and no partial one. The mechanism is unbuilt on this release train:

- there is no `curie adapter` verb family in the CLI surface
  (`cli/command-manifest.json`), and no adapter manifest schema anywhere;
- the only entry-point group declared in the repo is the harness one,
  `ENTRY_POINT_GROUP`
  (`runner/src/curie_runner/harness/registry.py::ENTRY_POINT_GROUP`); no
  `curie.<port>` sibling group exists;
- there is no service-registration table, no adapter address or endpoint
  configuration, and no channel-neutral egress port;
- `packages/channel-protocol` holds the neutral reply DTOs — `OutboundMessage`
  (`packages/channel-protocol/src/channel_protocol/models.py::OutboundMessage`)
  and `ChannelCapabilities`
  (`packages/channel-protocol/src/channel_protocol/models.py::ChannelCapabilities`)
  — which are the second rung of the ladder above, but it carries no conformance
  kit.

The nearest existing thing is per-turn and pre-dates this decision:
`ReplyHandle` (`packages/aci-protocol/src/aci_protocol/turn.py::ReplyHandle`)
carries a per-turn reply endpoint, which is what lets the first-party CLI stub
and a real Slack workspace coexist on one worker (#19). ADR-0096 promotes that
stub in status: it is the existing second channel implementation and becomes the
reference adapter the first conformance kit grows from.

## Known leakage

A NONE seam has no implementation to leak, so what belongs here is the distance
between the recorded intent and the tree, and the things a first implementer
would trip over:

- **The credential rule the decision states is violated by today's egress.**
  `AsyncSlackSink` (`apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink`)
  delivers every per-turn reply through one client holding a single Slack bot
  token, presented to whatever endpoint the turn names, and the
  unreachable-endpoint fallback re-sends that reply's content to the default
  transport. Both are correct for a one-workspace install and wrong the moment a
  vendor endpoint coexists with real Slack: the vendor would be handed the
  platform's token, and an outage would leak a vendor turn's reply into Slack.
  ADR-0096 writes this down precisely because it is a prerequisite, not a
  follow-up.
- **The binding surface is still a literal Slack column.** `Agent`
  (`apps/api/src/curie_api/models.py::Agent`) binds an agent by `slack_channel`,
  and the API's validators reject ids that are not Slack-shaped, so a vendor
  would today have to mint Slack-shaped channel ids exactly as the CLI stub does.
  The rename is a migration plus a contract change, not a refactor.
- **The interactivity return path has no scoped credential.** Approval resolution
  sits behind the single platform-wide key, and the scoped token minted for the
  sandbox is deliberately rejected everywhere but the state router. A scoped
  adapter credential in that shape is a prerequisite of a second channel adapter
  rather than a follow-up to one.
- **The decision is Accepted while the mechanism is not built, and its own
  acceptance header names realizing code paths that only partly exist here.** The
  ADR's Consequences section is explicit that it authorizes no code and ships
  without a reference third-party adapter, and the items listed under
  "Implementations today" above are what a reader will and will not find in the
  tree. Treat this file as the record of the intended line, not as evidence that
  a plugin mechanism is available.

## Cross-links

- **Related seam:** [harness-package](../harness-package/INTERFACE.md) — the only entry-point plugin mechanism in the codebase, and the model ADR-0096 generalizes from while reserving it for the narrow in-process exception.
- **Related seam:** [connector-host](../connector-host/INTERFACE.md) — the declare-and-host split one scope down, and the hosting substrate an operator-run adapter would reuse.
- **Related seam:** [channel-ingress](../channel-ingress/INTERFACE.md) — the port promoted first, and the one whose grade this decision is trying to move.
- **Related seam:** [channel-interaction](../channel-interaction/INTERFACE.md) — the neutral interaction primitives that are already the second rung of the promotion ladder.
- **Epic(s):** #19 — per-turn reply endpoint routing, which the worked example builds on; #158 — multi-tenancy, deliberately out of scope until it settles what a tenant owns
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — the standing restraint that no speculative adapter layer is written ahead of a real second implementation; not one of the six swap-readiness Jobs, so not separately graded
- **ADR(s):** [ADR-0096](../../adr/0096-port-adapters-are-deployed-services.md) — a third-party port adapter is a deployed service, not a loaded plugin; [ADR-0060](../../adr/0060-the-harness-is-a-declared-package.md) — the harness registry it generalizes; [ADR-0086](../../adr/0086-bundles-declare-connectors-the-platform-hosts-them.md) — the declare-and-host precedent moved up one scope; [ADR-0040](../../adr/0040-adopt-acp-as-an-edge-projection.md) — the trust rule inherited verbatim: an adapter is a rendering and transport contract, never a trust boundary
