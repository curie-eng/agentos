# 112. An installation owns the model gateway and agents select its aliases

Date: 2026-08-23

Status: Draft

Issues: #24, #473

If accepted, this ADR amends ADR 0037 only to permit an installation owned
gateway under the session pinning and observability rules below.

## Context

Curie currently points a runner at one model provider through its boot
environment. An operator can set a model, a credential, and an
Anthropic Messages compatible base URL. A per agent model override can replace
the model name, but it cannot safely choose a different endpoint or credential.

That is a good boundary for a direct provider, but it makes a model gateway
awkward. An operator who runs LiteLLM or another compatible gateway should be
able to configure it once and let agents use its named model pools, fallback
policy, provider selection, and optional classification. Agent authors should
not need to learn provider credentials or endpoint details merely to ask for a
fast, coding, reasoning, or automatic route.

The existing optional LiteLLM sidecar shows that the Claude runner can reach an
Anthropic Messages compatible gateway. It is a per sandbox escape hatch,
however, not a durable ownership and policy boundary. It also cannot make an
arbitrary gateway URL or credential safe to accept from an agent declaration.

ADR 0037 chose a model at sandbox claim time and rejected unconstrained
per request routing because changing models during a session damages prompt
cache economics and can make provider specific conversation state invalid.
That remains the default. A gateway introduces a useful exception only if its
route remains sticky for the session and its resolved model is observable.

## Decision

**An optional model gateway is installation owned. Curie agents select only a
named, installation allowed gateway alias.**

The installation declaration gains one optional gateway entry. It records the
gateway's base URL, its declared wire protocol, the named Secret reference for
the gateway credential, an allowed alias set, and one default alias. Curie
opens egress only to the declared gateway origin. The initial supported wire
protocol is Anthropic Messages because the current Claude harness speaks that
protocol. A later OpenAI or Responses harness may add another declared protocol
without changing this ownership boundary.

Curie delivers only the gateway credential and gateway base URL to a gateway
backed sandbox. Provider credentials remain on the gateway side. A direct
provider credential and a gateway credential are distinct installation secrets;
neither is stored in a bundle, agent row, ACI event, command output, or trace.

An agent uses the existing model selection surface to request an allowed alias,
for example `fast`, `code`, `reasoning`, or `auto`. An agent with no model
selection uses the installation default. The worker validates an override
against the selected installation's allowed aliases before a claim is created.
It never accepts a bundle supplied gateway URL, gateway credential reference,
or arbitrary alias. This keeps endpoint and credential authority with the
operator and prevents an agent declaration from becoming a request forgery or
credential redirection primitive.

An automatic alias is permitted, including a LiteLLM automatic router or
classifier, only under this contract:

1. The gateway resolves the alias to one concrete model and deployment on the
   first model request of a Curie session.
2. That route stays pinned through the session and through Curie's cold
   suspend and resume lifecycle. A gateway must either receive stable session
   identity and enforce affinity or return a durable resolution that Curie can
   replay. A gateway that can reclassify each turn is not suitable for Curie.
3. The gateway response exposes the resolved provider and model identity. Curie
   records that identity alongside the requested alias, so evaluations, costs,
   and incident investigation do not report only `auto`.
4. A classifier failure, unavailable chosen model, or unavailable route must
   either use the alias's declared deterministic fallback or fail the turn with
   an actionable gateway error. It must never silently fall back to fake mode
   or an undeclared provider.

Curie's install time model router from ADR 0037 remains the preferred mechanism
when Curie itself needs to make an auditable capability decision. The gateway
alias is the operator's provider aggregation and reliability policy. It does
not replace the binding hook, its registry, or its cheapest above floor
selection. A binding decision may choose an allowed gateway alias, and the
gateway may then choose among the operator's deployments behind that alias.

The present LiteLLM sidecar remains an optional development or isolated
deployment topology. A shared gateway is the normal production topology: it
centralizes provider credentials, routing configuration, budgets, and health.
Curie treats LiteLLM as the first gateway implementation, not as a frozen
platform dependency or as a Python library embedded in the runner.

## Consequences

The easy operator path becomes: configure the gateway once, declare an allowed
set of model aliases, and set an agent's model override to one alias. The easy
agent path contains no provider URL and no provider key.

Implementation must extend the installation configuration and render an
operator supplied gateway Secret only where the runner needs it. It must
preserve the existing direct provider path byte for byte when no gateway is
configured. The gateway configuration needs doctor validation before a claim,
including a Messages protocol probe, alias availability probe, and a clear
credential failure. The model selection surface needs alias validation on both
the API persistence boundary and the CLI mirror.

The first implementation must prove a real gateway path with the Claude Agent
SDK: streaming, tool use, a multi turn continuation, interrupt and cold resume,
an upstream failure, and an unauthorized gateway credential. The test must
show that the requested alias and actual resolved model are different where
automatic routing is enabled, and that the same concrete route is retained
after resume. It must also prove that an agent cannot redirect the runner to an
unapproved origin or cause a provider credential to enter the sandbox.

LiteLLM feature compatibility is version sensitive. Curie must pin and test a
known compatible gateway image before naming any automatic routing behavior as
supported. A model classifying gateway that cannot honor session affinity,
return a resolved model identity, or preserve Anthropic Messages streaming is
not supported for the Claude harness.

## Alternatives considered

### Let every agent configure its own provider or gateway

Rejected. It duplicates provider setup, makes credentials difficult to scope,
and permits arbitrary endpoint selection from data that is eventually supplied
by an agent author. It also prevents an operator from setting one budget,
egress, and failover policy.

### Embed LiteLLM as a runner dependency

Rejected. The runner is a harness adapter and speaks one provider wire protocol.
Embedding a large provider translation and routing library there would couple
every harness to one gateway implementation and put provider credentials in
every sandbox. A gateway remains independently deployable and replaceable.

### Only support the existing LiteLLM sidecar

Rejected. A sidecar starts with every sandbox, duplicates routing state and
provider credentials, and makes shared budgets, health, and routing policy
harder to operate. It remains useful where an installation intentionally wants
isolation, but it is not the primary gateway topology.

### Let automatic routing select a new model for every turn

Rejected. It contradicts ADR 0037's session pinning rule, invalidates provider
prompt caches, and can replay provider specific history to an incompatible
model. Classification may select the first route only, subject to the sticky
route and observability requirements above.

### Make a gateway alias the only model routing mechanism

Rejected. A gateway can optimize provider delivery, but Curie still needs a
portable, auditable binding decision for manifest intent, capability floors,
and deployment level policy. The two layers select at different boundaries and
remain complementary.
