# 98. Thinking depth is an operator knob, never a bundle one

Date: 2026-08-04

Status: Accepted

## Context

Issue #1182 reported that OpenRouter turns take far longer than Anthropic ones.
The cause is not the provider. It is that the model on the other end reasons
before it answers, and Curie neither shows that nor lets anyone change it.

Measured against the endpoint Curie actually dials — OpenRouter's
Anthropic-compatible `/v1/messages` — same prompt, same 2048-token ceiling:

| model | wall clock | output tokens | thinking tokens |
|---|---|---|---|
| `anthropic/claude-sonnet-4.5` | 2.4s | 95 | **0** |
| `z-ai/glm-5.2` | 8.1s | 380 | **268** (70%) |
| `z-ai/glm-5.2` with thinking disabled | **1.7s** | 182 | **0** |

`z-ai/glm-5.2` is not unusual: `deepseek/deepseek-r1` defaults to 259 thinking
tokens and `qwen/qwen3-235b-a22b-thinking-2507` to 613. Reasoning-on is simply
the default for that class of model, while `claude-sonnet-4.5` defaults it off.
The reporter compared one of each.

**Curie sets nothing here.** The twelve fields the runner hands
`ClaudeAgentOptions` (`runner/src/curie_runner/adapter.py`) carry no thinking
configuration, so whatever the model ships with is what runs. The SDK does
expose the control — `thinking`, `effort`, and the deprecated
`max_thinking_tokens` — Curie has simply never named it.

So an operator who is paying for a reasoning model has no way to say "not this
deep, not on this agent" and no way to see where the time went. Disabling
thinking on that measurement is a **4.8x** latency change, which is a decision
worth being able to make.

The question this ADR settles is not *whether* to expose it. It is **who is
allowed to set it**, because "how much compute this agent burns per turn" is
exactly the kind of authority Curie has been deliberate about elsewhere.

## Decision

**Thinking depth is operator-owned, in the same two layers as the model, and a
bundle cannot set or influence it.**

```
platform default   CURIE_THINKING on the worker      (WorkerConfig)
per-agent override agents.thinking column            (set through the platform API)
bundle             — no surface, at any tier
```

This is not a new mechanism. It is the shape `model` already has, and it reuses
`apply_model_env`'s existing override precedence verbatim: the per-agent value
wins when set, otherwise the platform default applies, otherwise nothing is sent
and the model's own default stands (which is today's behavior exactly, so an
install that configures nothing sees no change).

The value travels to the runner as a **declared `BootEnv` key**, not an
undeclared env var. `BootEnv` is the single declaration of the worker-to-runner
boot contract (ADR-0049), and `apps/worker/tests/binding/test_boot_env_single_declaration.py`
states the rule without an exception: a key read or written *for the same
consumer as the boot contract* — the runner in a sandbox — must be named from
`BootEnv.env_key`. `thinking` is a sibling of `model`, with the same producer and
the same consumer, so it is a `BootEnv` key on the same grounds.

The contract stays permissive about the value and the **runner owns the
vocabulary and the validation**, mirroring `api_backend` (#514): the field is
`str | None` in `BootEnv`, while `ApiBackend` and its rejection live in
`runner/src/curie_runner/sdk_auth.py`. The contract should not mirror
claude-agent-sdk's option shape; a harness swap must not be a protocol change.

### Why the operator and not the bundle

The precedent is already written down. `apps/worker/src/curie_worker/binding.py::apply_model_env`
says which knobs an agent may carry and why the others cannot:

> `model_override` is the per-agent `CURIE_MODEL` (#254) … **It is the ONLY
> per-agent knob here.** The `api_backend` and `env_key` declarations (#514) come
> from `WorkerConfig` only and take no override … so **a lower-privileged agent
> author must not be able to set them.**

Three things follow, and together they settle it:

- **A bundle cannot even choose its model.** `packages/plugin-format` has no
  model field; the per-agent value is a column on the platform's `agents` row,
  written through the API by an operator. Thinking depth sits on the same
  capability-versus-cost axis and is *less* consequential than the model choice
  itself. If a bundle author may not pick Opus over Haiku, they may not pick how
  deeply that model reasons.
- **A bundle is untrusted input.** It executes in a sandbox. Anything it declares
  is lower-privileged *by construction*, whoever wrote it — the same reasoning
  that stops an agent widening its own approval policy.
- **Every other cost control here is already operator-side**: `max_usd_per_day`,
  `max_output_tokens_per_run`, budgets, the kill switch. A thinking knob owned by
  the bundle would be the only one that is not.

The asymmetry of the failure modes points the same way. A bundle that turns
thinking up is a bill the operator discovers after the fact; an operator who
turns it down sees the quality cost in their own evals. Put the control with the
party who can observe the consequence.

## Consequences

**An install that configures nothing behaves exactly as it does today.** Unset
means the field is never sent and the model's own default stands. This ADR adds
an option; it does not change a default.

**The contract moves, so it lands first and on its own.** Adding a `BootEnv`
field changes `packages/aci-protocol`, which `AGENTS.md` makes a stop-and-
escalate: a contract change lands as its own reviewed change *before* dependent
lanes proceed. It is the lightest class the semver table has — a new optional
field an old consumer ignores is a **patch** under 0.x, `0.2.8` -> `0.2.9` — but
it is still its own change, so this ADR ships with the field and nothing that
consumes it.

**The knob is real only once the runner reads it.** Between the contract change
and the runner change the key exists and does nothing. That is the intended
sequencing, not an oversight.

**A bundle author who genuinely needs deeper reasoning has no recourse but to
ask their operator.** Accepted deliberately: no such case exists yet, and
`AGENTS.md`'s own restraint rule ("a second implementation is what earns an
abstraction") says not to build the advisory channel for a hypothetical one.

**Nothing here makes the cost visible.** Reporting thinking tokens into traces
is a separate, unblocked change; this ADR is about control, not observability.

## Alternatives considered

**Let the bundle declare it, advisory, with the operator overriding.** The most
tempting middle: the author knows whether their task needs depth. Rejected for
now on two counts. It needs a new field in `packages/plugin-format`, a frozen
contract whose changes are stop-and-escalate in their own right, and there is not
yet one real case of an author needing it. Revisit when there is; that is a new
ADR, not a footnote to this one.

**Ship a default (disable, or cap, reasoning).** Rejected. Curie currently sends
nothing, so "changing the default" means Curie starting to make this decision for
every model on every install. Reasoning is what a reasoning model is *for*, and
Curie's own use case — multi-step tool loops, judgement, self-correction — is
where it earns its cost. Trading that for latency, silently, for everyone, is not
ours to do. The measured 4.8x is an argument for offering the choice, not for
making it.

**Expose `effort` alongside `thinking`.** Deferred, not rejected. `effort`
(`low`..`max`) is a real SDK control and a plausible second dial, but the
measurement above verified `thinking`, not `effort`, on the third-party
endpoints that motivate this. Shipping an unverified knob would put a control in
operators' hands without knowing it does anything on the providers they use.
Additive later, and cheap once someone verifies it.

**Route it through a generic operator-config passthrough instead of a declared
key.** Rejected on the boot-contract rule quoted above, and on the reason behind
it: a name typed on both sides is exactly the drift #488 closed, where a rename
leaves the sandbox booting fine and the feature silently gone.

## Approval

Accepted by Yichen Zhang (@yichenzhang-curietech) on 2026-08-04, on the shape
proposed above: operator-owned, two layers mirroring `model`, no bundle surface.
