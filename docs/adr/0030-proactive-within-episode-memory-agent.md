# 30. A proactive within-episode memory agent over the frozen ACI injection seam

Date: 2026-07-14

Status: Draft

Extends [ADR-0025](0025-memory-port-and-first-loader.md) (the memory port and its
passive cross-session preamble) and [ADR-0003](0003-stateless-first-rehydrate-on-resume.md)
(stateless-first, rehydrate on resume); built on
[ADR-0005](0005-claude-agent-sdk-adapter-and-frozen-aci.md) (the frozen ACI
inject/steer primitive) and composing with
[ADR-0010](0010-approval-gates-and-human-in-the-loop.md) (loop-boundary
interception). Supersedes none. Motivated by *Remember When It Matters: Proactive
Memory Agent for Long-Horizon Agents* (Wu et al., Meta AI, arXiv:2607.08716),
whose target workload, long-horizon terminal and coding agents, is exactly
[ADR-0021](0021-curie-is-a-harness-for-coding-agents.md)'s.

## Context

Curie has a memory model, and the paper names the precise way it leaves value on
the table for long runs.

**Our memory today is passive, and passive is the weak baseline.** ADR-0025
delivers memory as durable **cross-session lessons** loaded at boot and composed
into the system prompt as a **preamble**: a static block the model sees once, at
the start, and thereafter competes with a growing trajectory for attention. The
paper's central measured finding is that this shape underperforms. Exposing the
full memory bank at every step ("Full-bank context") **trails a selective
intervention policy by 2.8 macro points** on τ²-Bench, and a general
retrieval-into-context memory layer (Mem0) does not beat the baseline on the
hardest domain at all. Passive exposure is not free; the paper: *"surfacing too
much memory adds latency, consumes tokens, and can distract the agent."*

**The failure it targets is one long coding runs actually hit.** The paper names
**behavioral state decay**: during long-horizon execution, information that should
shape the next action (a task requirement, an environment fact, a failed attempt,
a diagnosis) *"stops influencing behavior. The information may still be present in
the transcript, or may even remain within the model's context window, but it no
longer exerts reliable control over behavior."* The concrete patterns are exactly
our target agent's: satisfy a requirement early then violate it while fixing an
unrelated bug; retry a near-identical command that already failed; re-diagnose an
error already diagnosed. A boot-time preamble (ADR-0025) cannot fix this, because
the information *is already in context*. The problem is *when* it becomes active,
not whether it is stored.

The mechanism is, crucially, **within one episode** and **plug-and-play with an
unmodified action agent**, which maps onto seams Curie already has frozen. A
separate memory agent observes the trajectory, maintains a structured bank, and at
each step chooses to **inject a targeted reminder or stay silent**. The action
agent's instructions, tools, and decoding are unchanged; the only integration
point is an optional transient context block on the next call. That injection is
precisely what ADR-0005 already proved the ACI can do: a mid-run message *"steered
a tool-using agent at the next loop boundary."* A memory reminder *is* a steer at a
loop boundary. The gains are real for our exact benchmark: **+8.3pp on
Terminal-Bench 2.0** and **+6.8pp on τ²-Bench** for a Sonnet-class action agent.

## Decision

**Add a proactive memory agent as an optional, plug-and-play layer over the frozen
ACI injection seam. It is a *within-episode* execution-state layer that composes
with, and does not replace, ADR-0025's cross-session lessons and ADR-0003's
resume.** Draft. The validation gate below informs maintainer acceptance.

- **A separate memory agent, not a harness change.** The memory agent runs as its
  own process alongside the action-agent runner (our one-process-per-sandbox shape
  already isolates it, ADR-0005). It observes the trajectory the runner already
  externalizes (the ACI NDJSON frames or the Langfuse trace, ADR-0004) over a
  recent window, and it reaches the action agent only through the existing ACI
  inject/steer primitive. No `aci-protocol` change, no runner adapter change: this
  is the paper's "unmodified action agent" claim expressed in our contract.

- **A within-episode memory bank, distinct from ADR-0025's lessons.** The bank has
  the paper's three parts: a **private status** field (never shown to the action
  agent), **knowledge** entries (stable task and environment facts, paths, verified
  results), and **procedural** entries (attempts and outcomes: failed commands,
  successful fixes, ruled-out hypotheses). It is maintained by explicit tool-call
  edits, not free-form rewriting. This is *execution state for the current run*,
  where ADR-0025 is *durable lessons across runs*: orthogonal and composable.

- **Selective intervention is the point, silence is a first-class action.** At each
  step the memory agent emits either a targeted reminder or an explicit
  no-intervention. The ablations are unambiguous that this is where the gain lives:
  selective intervention beats full-bank exposure, beats always-inject on the
  domain-balanced average, and beats advisor-only (no bank). Per the paper, *"the
  most balanced gains come from combining maintained execution-state memory with a
  selective intervention policy."* We therefore do **not** ship an always-on
  injector; the policy must be able to stay silent.

- **The bridge to ADR-0025 is one-directional and deferred.** A run's durable
  *procedural* entries (a fix that worked, a dead-end ruled out) are exactly the
  raw material for ADR-0025's cross-session lessons. Feeding them into the
  learned-record **extraction** path (#265/#266/#267) is the natural join, but it
  is out of scope here and named as future work. This ADR lands the within-episode
  layer only.

### Validation gate for maintainer acceptance

Per ADR-0001, a maintainer considers evidence from a spike on a real bundle
driven through `local`/`cluster` before accepting this Draft: (1) the reminder
actually lands as a steer at the next loop boundary via the existing ACI path with
no harness modification; (2) an eval delta on a long-horizon case set attributable
to the memory layer (graded by ADR-0022's trajectory graders or ADR-0042's
verifier); and (3) a token and latency accounting for the second (memory-agent)
model call, which the paper acknowledges but never benchmarks.

## Alternatives considered

- **Just make ADR-0025's preamble richer, or retrieve into context.** Rejected:
  this is the passive shape the paper measures as the weaker baseline (full-bank
  context, Mem0). More or better-retrieved context in the preamble does not fix
  behavioral state decay, because the decayed information is already in context.
- **An always-on injector (drop the silence option).** Rejected: the always-inject
  ablation is competitive on the micro-average but loses on the domain-balanced
  macro-average and adds a reminder's tokens every step. Silence-as-an-action is
  the calibration the gain depends on.
- **Fold this into ADR-0025's memory port.** Rejected: ADR-0025's `MemoryStore`
  is a load/append store for cross-session records with provenance; the
  within-episode bank is different state with a different lifecycle (it lives and
  dies with the run) and a different delivery path (loop-boundary injection, not a
  boot preamble). Conflating them would overload one port with two lifecycles.
- **Train our own memory-agent model now (the paper's SFT+GRPO recipe).**
  Rejected as premature: the paper's own trained-27B result is explicitly
  "preliminary," and a prompted frontier model as the memory agent is what
  produced the headline +8.3pp and +6.8pp. Start prompted; a trained memory agent
  is later headroom, not the first step.

## Consequences

- **Cost is a second model call per memory step.** The memory agent is an
  additional inference stream (the paper uses an Opus-class model as the memory
  agent for a Sonnet-class action agent). The selective and silent design is
  partly a cost control, but the token and latency budget is unquantified in the
  paper and is a spike deliverable. This is why the layer is opt-in per agent, not
  a default.
- **Cross-session persistence of the bank is our problem, not the paper's.** The
  paper is entirely within-episode and says nothing about surviving suspend/resume.
  ADR-0003 governs that seam: if a suspended long run should keep its
  execution-state bank, that is an explicit design step here (externalize the bank
  like ADR-0025 externalizes lessons), not something the paper hands us. Absent
  that, the bank is reconstructed on resume from the rehydrated history, which is
  acceptable for a first slice and named as a limitation.
- **It shares interception machinery with ADR-0010.** Both the memory reminder and
  the approval gate act at the loop or tool boundary, and the paper notes
  interventions cluster *"immediately before a state-changing tool call."* The two
  should share the injection/interception path rather than grow two parallel
  mechanisms.
- No frozen-contract change: the layer rides the existing ACI inject/steer
  primitive and the existing trace stream. That is the point. It is additive and
  removable without touching `aci-protocol` or `plugin-format`.
