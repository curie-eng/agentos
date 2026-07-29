# 44. Workflow-controlled agents run on Curie as a callable substrate first, a unified plane only on demand

Date: 2026-07-16

Status: Draft

This is a **decision, not code**: it settles whether, and how deeply, Curie
supports *workflow-controlled* agents — developer-authored orchestration
frameworks such as LangGraph and CrewAI — and what we would build to do it.

## Context

There are two agent-architecture shapes. **Model-controlled**: the model drives
an agentic loop and decides the next action each turn. This is Curie's default
and the thing the whole platform is built around — Claude Code (and the OpenCode
spike) sit behind the turn-based `ModelSession` port, per ADR-0021 ("Curie is a
harness for coding agents") and ADR-0011. **Workflow-controlled**: the developer
explicitly encodes the control flow as a graph or a flow, and the LLM is called
only inside the nodes the developer wrote. Predictability is the point; it matters
most for autonomous agents, where no human is in the loop to catch a
model-chosen path that goes sideways. LangGraph and CrewAI are the reference
implementations of this shape.

The question this ADR closes: does Curie support workflow-controlled agents,
and if so, at what depth?

Facts that constrain the answer (all verified against current framework docs and
source, 2026-07-16):

- **LangGraph and CrewAI are libraries, not harnesses.** CrewAI's `kickoff()` is a
  synchronous call the entrypoint invokes and exits; there is no CrewAI daemon.
  LangGraph is the same when embedded (`builder.compile().invoke()/.astream()`),
  with an optional server. Neither wants to own the process lifecycle. **They do
  not fit the `ModelSession` port**, which is one model-driven *turn*
  (`query` → `receive_turn` → result). A graph/flow run is an arbitrary
  multi-step program, not a turn — forcing it through the turn contract loses the
  per-step interrupt/steer semantics.
- **Control flow is developer-authored and deterministic** in both: LangGraph
  `StateGraph` (nodes + conditional edges); CrewAI **Flows** (`@start`/`@listen`/
  `@router`), with Crews as the opaque unit of work a Flow step invokes.
- **Events are in-process Python, not a network protocol.** LangGraph's current
  surface is `stream_events(version="v3")` (typed projections: `stream.tool_calls`,
  `message.text`, final); CrewAI's is the event bus (`BaseEventListener` on
  `crewai_event_bus`). Any translation to a wire protocol must run *in-pod*.
- **Durable pause/resume exists but distributed coordination does not.** LangGraph:
  `interrupt()` + `Command(resume=...)` + `PostgresSaver`, resume by `thread_id`,
  with no locking (two pods resuming the same thread double-execute). CrewAI: Flow
  `@human_feedback` + a custom async provider raising `HumanFeedbackPending` +
  `@persist`. Neither enforces the authorization decision server-side, protects
  against spoofing from inside the agent's box, nor prevents double-execution.
- **Native evals are thin, and the real eval tooling is free and standalone.**
  LangGraph agents are evaluated with `agentevals`/`openevals` — open-source pip
  packages that run trajectory-match and LLM-as-judge **without a LangSmith
  account**. CrewAI has `crew.test()` (per-task LLM-judge, 1–10). Both score the
  same shape Curie's own `TrajectoryScorer` scores (ADR-0022).
- **Telemetry is OTLP-reachable without us.** OpenInference and OpenLLMetry emit
  standard OTLP for both frameworks to any collector; CrewAI reaches arbitrary
  collectors via OpenLIT. (CrewAI also phones home to `telemetry.crewai.com:4319`
  by default — an egress target to block, per ADR-0032.)
- **Both frameworks have their own vertical.** LangGraph Platform (server +
  Postgres + Redis + eval UI + tracing) and CrewAI AMP / Enterprise "Factory"
  (Helm-deployed) are commercial, license-gated products. The library is
  OSS; the hosted vertical that runs it in production is not. **This is what
  Curie would actually compete with** — not the free library.

Three integration shapes fall out of these facts:

- **A — bare substrate.** Run the framework process in the sandbox; provide
  egress/secrets/k8s/security. The developer owns orchestration, evals, telemetry.
- **B — ACI adapter (unified plane).** Ship a per-framework in-pod shim that
  translates the framework's event stream into the Curie ACI wire protocol, so
  `TrajectoryScorer` evals, side-effect gating, and telemetry work over a
  framework-authored workflow — one control plane across a heterogeneous fleet.
- **C — inverted (callable step).** The developer's deterministic workflow is the
  *outer* loop; at each open-ended-AI or gated-side-effect step, a node/task calls
  *into* a Curie sandbox to run a model-controlled turn as one unit of work.

Prior decisions frame this:

- **ADR-0016** (swappable jobs around an opinionated core): a seam is promoted to a
  marketed, frozen contract only when a real swap demand arrives, not
  speculatively. The second implementation teaches the interface.
- **ADR-0028** (substrate is resilience, not a product swap axis): the same
  discipline applied to the sandbox seam — keep a clean port, but do not market it
  as a bring-your-own axis ahead of funded demand.
- **ADR-0031** (harness-neutral runner seams, *Proposed*): the runner is still
  Claude-shaped at four seams (`translate.py` isinstance-matches SDK types, the
  side-effect allowlist hardcodes Claude tool names, plugin ingestion hands the
  bundle to the SDK). B depends on this refactor landing first.
- **ADR-0033** (scoped-HMAC approval token) and the approval seam work (#411–#419):
  server-side, spoof-resistant, coordinated human approval — precisely the
  guarantee the framework verticals lack.

## Decision

**Curie supports workflow-controlled agents by being the substrate their
workflow runs on and calls into — an open, hardened, self-hosted alternative to
the vendor vertical — NOT by swapping LangGraph/CrewAI in as a harness behind
`ModelSession`.** We commit to **C now** and hold **B as a demand-gated phase 2**.
A is real but is never the pitch.

Concretely:

1. **Lead with C (the callable step). It is the phase-1 build.** C requires no
   `ModelSession` refactor, no per-framework event translation, and no dependency
   on ADR-0031. The developer keeps their LangGraph/CrewAI workflow verbatim; at
   the steps that need open-ended AI or a gated side effect, they call the
   existing Curie API to run a sandboxed, approval-gated turn and fold the
   result back into their graph state. This delivers the two things Curie has
   that the vendor vertical does not — a gVisor-hardened, egress-controlled
   sandbox (free the moment the process runs in the pod, per A) and secure
   coordinated approval (ADR-0033) — at near-zero incremental build cost.

2. **A is the floor, not the story.** Running the framework process in the
   sandbox with egress/secrets/security is genuine and comes for free, but "a
   hardened pod for your agent framework" is undifferentiated from any Kubernetes
   pod. It is never marketed as the value proposition on its own.

3. **B is deferred and explicitly gated.** The full ACI adapter — the "single pane
   of glass over a heterogeneous agent fleet" — is built only when **all** of:
   (a) ADR-0031's harness-neutral seams have landed (B has no clean home until
   then); (b) a concrete customer needs Curie to be the single eval/observability
   plane over framework-authored *and* model-controlled agents together; and
   (c) Curie's eval story is demonstrably better than the free, standalone
   `agentevals`/`openevals` — otherwise B re-plumbs tooling the framework user
   already has. Absent (b) and (c), B duplicates free capability at high cost.

4. **The comparison is against the vendor vertical, not the OSS library.** B's and
   C's value is measured against LangGraph Platform / CrewAI AMP — where Curie
   wins on open/self-hosted, sandbox hardening, and secure coordinated approval —
   not against the free `kickoff()`/`compile()` library, against which we add
   nothing.

### Recommendation (the decision, stated plainly)

**Be the secure substrate a deterministic workflow calls into, first; become its
unified eval/telemetry plane only when a customer pays for that and our eval beats
the free tools.** Skills do not transfer (there is no skill concept in these
frameworks) and telemetry stays the developer's, reachable through the OTLP
collector Curie already runs. The differentiators are the sandbox and the
approval gate — and C delivers both without the B plumbing. Build C; hold B behind
ADR-0031 and demand.

## What building B would actually take (for when the gate opens)

B is not "write an adapter" — it is a second runtime path through the runner:

1. **ADR-0031 lands first** (the `TurnEvent` union + `BundleInstaller` port +
   harness-declared tool identity). Biggest hidden cost; currently *Proposed*.
2. **A non-turn-based runner seam** distinct from `ModelSession` — "run this whole
   program, stream ACI events" — since a flow is not a turn.
3. **Per-framework in-pod translators**: `stream_events(v3)` → ACI for LangGraph,
   `crewai_event_bus` listener → ACI for CrewAI.
4. **Tool-identity mapping** so the side-effect classifier does not misfire on
   non-Claude tool names (the ADR-0031 item-2 problem, one level worse).
5. **Approval bridge**: map `interrupt()` / `HumanFeedbackPending` onto the durable
   `Approval` record + server-side authorizer + resume reconciler + SKIP-LOCKED
   coordination (#411–#419, ADR-0033).
6. **Packaging**: framework apps are not Claude plugin bundles — needs the
   `BundleInstaller` port or a plain-container path.

## Alternatives considered

- **Swap LangGraph/CrewAI in as a second harness behind `ModelSession`** (the
  OpenCode slot). *Rejected:* category error. `ModelSession` is a single
  model-driven turn; a graph/flow is an arbitrary multi-step program. Forcing it
  through the turn contract discards the per-step interrupt semantics the approval
  system needs and gains nothing.

- **Build B now as the headline "unified agent plane."** *Rejected for now:* per
  ADR-0016, a seam is built and frozen on real demand, not because it looks
  compelling. Most of B (evals, telemetry) duplicates free, standalone framework
  tooling; the genuinely differentiated parts (sandbox, secure approval) are
  delivered by C at a fraction of the cost. B also has no clean home until
  ADR-0031 lands. Left as an explicitly gated phase 2.

- **Market A ("bring your framework, run it in our pod") as the product.**
  *Rejected:* undifferentiated from a bare Kubernetes pod; adds nothing over the
  framework's own OSS library and does not touch the vendor vertical's actual
  value (managed HITL, eval UI, tracing).

- **Do nothing / stay model-controlled only.** *Rejected:* workflow-controlled is
  a real and growing architecture for autonomous agents, and C lets Curie serve
  it with capability the platform already has. Declining it forfeits the
  deterministic-workflow segment for no saving, since C is nearly free.

## Consequences

- **Phase 1 is scoped to C** and reuses the existing sandbox + deploy/message API
  and the approval seam; it does not touch `ModelSession`, `translate.py`, or the
  ACI. It can proceed independent of ADR-0031.
- **B is a named, gated future**, not an open question: the gate is ADR-0031 +
  single-plane customer demand + a differentiated eval. A PR that builds a
  per-framework ACI adapter ahead of that gate is violating this ADR, the same way
  ADR-0028 flags a speculative substrate.
- **Skills are out of scope for both** and this is recorded, not treated as a gap:
  the frameworks have no skill concept, so there is nothing to port.
- **Telemetry stays the developer's**, with Curie offering its OTLP collector as
  the sink (one `openlit.init()` / OTEL env var on their side). We do not build a
  worse path to telemetry via the ACI adapter.
- **Positioning is settled**: against the framework verticals Curie markets the
  hardened sandbox and secure coordinated approval (and, if B ships, the unified
  plane) — not "we can run your LangGraph."
- **Revisiting is expected**: a funded single-plane requirement, plus ADR-0031
  merged, triggers a superseding ADR that greenlights B and freezes the
  non-turn-based seam. The clean seam is what keeps that future cheap.
