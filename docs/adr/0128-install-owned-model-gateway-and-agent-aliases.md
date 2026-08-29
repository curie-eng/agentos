# 128. An installation owns the model gateway and operator bindings select its aliases

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

**An optional model gateway is installation owned. Operator-owned agent and
binding configuration selects only a named, installation-allowed gateway
alias; portable bundles continue to declare capability intent.**

The installation declaration gains one optional gateway entry. It records the
gateway's base URL, its declared wire protocol, the named Secret reference for
the gateway's root credential, an allowed alias set, and one default alias.
Curie opens egress only to the declared gateway origin. The initial supported
wire protocol is Anthropic Messages because the current Claude harness speaks
that protocol. A later OpenAI or Responses harness may add another declared
protocol without changing this ownership boundary.

Alias names are installation configuration, not a frozen Curie-wide vocabulary.
An installation may use arbitrary aliases that its compatible gateway supports,
including LiteLLM model aliases. Curie recommends the conventional names
`fast`, `code`, `reasoning`, and `auto` as useful defaults, but does not require
or reserve them. An installation may map those conventions, or any other
allowed alias, to its own model pools and routing policy.

The root gateway credential never enters a sandbox. A control-plane credential
broker uses it to mint a short-lived gateway token bound to the installation,
agent, session, selected alias, budget ceiling, and absolute expiry. The gateway
must enforce every scope; a gateway that only offers one reusable bearer key is
not supported for shared use. The sandbox receives only that scoped token and
the gateway base URL. Provider credentials remain on the gateway side. Direct
provider, root gateway, and scoped session credentials are distinct; none is
stored in a bundle, agent row, ACI event, command output, or trace. The scoped
token exists only in the runner boot secret surface for its session and is
redacted from diagnostics.

Gateway traffic is authenticated and encrypted. A public or routable gateway
uses HTTPS with certificate verification; an in-cluster gateway uses mTLS or an
equivalent workload-identity channel. Plain HTTP plus a bearer token is not a
supported exception. Curie opens egress only to that authenticated origin.

An operator uses the existing per-agent model selection or install binding to
choose an allowed alias, for example `fast`, `code`, `reasoning`, `auto`, or an
installation-defined name. An operator-owned agent with no override uses the
installation default. A portable bundle never names an alias: under ADR-0037 it
declares capability floors and constraints, and the install binding maps that
intent to an allowed alias or direct model. The worker validates the resolved
alias before a claim and before minting the scoped token. An absent alias is a
validation error and must never fall through to the default, another alias, or
a direct provider. No bundle supplies a gateway URL, alias, credential
reference, or token scope. This keeps endpoint, credential, and installation
vocabulary authority with the operator and preserves ADR-0037's portable intent
boundary.

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
4. Before the first route is pinned, a classifier failure or unavailable
   candidate may use the alias's declared deterministic fallback. Once one
   concrete provider and model is pinned, its unavailability fails the turn
   with an actionable gateway error; it never changes the session's route. No
   failure may fall back to fake mode or an undeclared provider.

Curie's install time model router from ADR 0037 remains the preferred mechanism
when Curie itself needs to make an auditable capability decision. The gateway
alias is the operator's provider aggregation and reliability policy. It does
not replace the binding hook, its registry, or its cheapest above floor
selection. A binding decision may choose an allowed gateway alias, and the
gateway may then choose among the operator's deployments behind that alias.

The present LiteLLM sidecar remains an optional development or isolated
deployment topology. For an installation that opts into a gateway, a shared
gateway is the normal production topology because it centralizes provider
credentials, routing configuration, budgets, and health; direct provider paths
remain the default when no gateway is configured. Curie treats LiteLLM as the
first gateway implementation to evaluate, not as a frozen platform dependency
or as a Python library embedded in the runner. It is supported for shared use
only if the pinned version can enforce the scoped-token contract above.

## Consequences

The easy operator path becomes: configure the gateway once, declare an allowed
set of model aliases, and set an operator-owned agent override or binding to one
alias. The recommended aliases simplify install configuration while the allowed
set preserves an installation's freedom to name and map its own pools. A bundle
contains no alias, provider URL, or provider key.

Implementation must extend the installation configuration with a root gateway
Secret available only to the control-plane broker; the runner receives only a
scoped session token. It must preserve the existing direct provider path byte
for byte when no gateway is configured. Doctor validates authenticated
transport, scoped-token minting and enforcement, Messages protocol behavior,
alias availability, and clear credential failure before a claim. The
operator-owned model selection surface needs alias validation on both the API
persistence boundary and the CLI mirror.

The first implementation must prove a real gateway path with the Claude Agent
SDK: streaming, tool use, a multi turn continuation, interrupt and cold resume,
an upstream failure, and an unauthorized gateway credential. The test must
show that the requested alias and actual resolved model are different where
automatic routing is enabled, and that the same concrete route is retained
after resume. It must also prove that an agent cannot redirect the runner to an
unapproved origin, use its scoped token for another agent/session/alias or past
expiry/budget, recover the root gateway credential, or cause a provider
credential to enter the sandbox. A control proves the pinned route fails rather
than switching when it becomes unavailable after the first request.

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
