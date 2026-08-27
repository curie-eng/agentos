# 133. A write connector declares permitted field paths, and the agent composes within them

Date: 2026-08-26

Status: Draft

Answers the gap named in the review of PR #1886: that a per-bundle check over one
write connector is not a model for connector authorization, and that the target is
builder-owned policy over the published tool surface where every operation is
denied, approval-required, or allowed, with unclassified operations denied.

Extends rather than replaces [ADR-0010](0010-approval-gates-and-human-in-the-loop.md)
(a human decides before a mutating call) and
[ADR-0117](0117-a-tool-that-changes-the-world-reports-what-it-changed.md) (the call
reports what it changed). It changes no isolation boundary: a sandbox reaches
nothing new, and [ADR-0006](0006-security-rails-as-chart-defaults.md)'s rails stand.

## Context

### What the write surface is today, measured

Interrogated against the four connectors of a running SRE bot install by MCP
handshake and `tools/list`:

```
kubernetes   13 tools (0 write)
grafana      48 tools (0 write)
tempo         4 tools (0 write)
k8s-write     1 tools (1 write)
```

Sixty-six published tools, exactly one not `readOnlyHint`, and the bundle's single
`approvalPolicy` gate names it. The write verb is `restart_deployment`, ceilinged to
three named Deployments.

That is a defensible surface. It is also the whole of it, and the question this ADR
exists to answer is what happens when someone asks for the second verb.

### Why the connector is the strongest ceiling, and what that costs

[`examples/sre-bot/manifests/write-role.yaml`](../../examples/sre-bot/manifests/write-role.yaml)
states the three ceilings in order of strength, and the order is the important part:

1. **The connector.** It emits only the restart annotation patch and exposes no tool
   taking an image, an env var, or a command.
2. **`resourceNames`.** The only narrowing RBAC offers.
3. **The approval gate.** A control on the agent, not on the credential.

The first is strongest because of one property, visible in
[`examples/sre-bot/connectors/k8s-write/server.py`](../../examples/sre-bot/connectors/k8s-write/server.py):

```python
# The patch body is constant apart from the timestamp. Nothing a
# caller supplied reaches it.
```

And it has to be strongest, because RBAC cannot help. The same file spells out why:
`kubectl rollout restart` is not a distinct permission -- it is a PATCH of the pod
template -- so `patch` on `deployments` is *exactly* the grant that permits

```
kubectl set image deploy/app app=totally-different-image
kubectl set env   deploy/app SOME_API_KEY=...
kubectl patch     deploy/app -p '{"spec":{"template":{"spec":{"containers":[{"command":[...]}]}}}}'
```

There is no RBAC expression that separates a restart from replacing what runs.

The cost of putting the ceiling in the connector is that a verb is a connector.
Two verbs today:

| connector | lines | published tools |
|---|---|---|
| `connectors/k8s-write` | 284 | 1 |
| `connectors/k8s-scale` | 296 | 1 |

580 lines for two operations, and the two files are near-duplicates: kubeconfig
loading, client construction, allowlist parsing, the `insecure-skip-tls-verify`
refusal, timeout handling, and the ADR-0117 prior/post report are written twice.
What differs is one tool function and one patch body.

The cost is not only lines. Each verb also brings its own image to build and publish,
its own `approvalPolicy` gate, and its own `K8S_*_ALLOWLIST` that must agree with the
Role's `resourceNames`. On the install this was measured against, `k8s-scale` **cannot
be deployed at the cluster tier at all**, because no image was ever published for it --
and a gate naming an absent connector fails bundle validation, so its gate has to be
removed with it. The second verb has been written, reviewed, and merged, and is still
not reachable. That is what "the cost is linear in verbs" looks like in practice.

### Why the agent cannot hold the decision

The obvious escape is a thin, general write tool -- `patch(resource, body)` -- with the
agent deciding what to send. It collapses the 580 lines to a few dozen and every future
verb to zero.

It also moves the ceiling from the connector to the agent, and the agent is the one
component in the system that is persuadable.

This is not a general argument about prompt injection. It is specific to what this
bot reads.
[`examples/sre-bot/manifests/read-access.yaml`](../../examples/sre-bot/manifests/read-access.yaml)
grants, cluster-wide, `pods`, `pods/log`, `events`, and `configmaps`. Verified on a
live install by `kubectl auth can-i --as=`: pod logs and config maps in every
namespace, `yes`. So the bot's ordinary working input is text that anyone who can
write a log line controls, and its ordinary output is a message in a chat channel.
The file's own header already reasons this way about why `configmaps` is in the list
and `secrets` is not: "This agent is prompt-injectable AND posts what it finds into a
chat channel."

Combine that read surface with an unconstrained patch body and the ceiling is no
longer "restart three Deployments". It is arbitrary code execution in the cluster,
reachable from a string an attacker put in a log.

The approval gate does not rescue it. With one tool, every call is gated, so the human
sees every patch -- and what they see is a model-authored patch body at 3am.
Draft ADR-0125 (PR #1934) names exactly this failure: the gate "cannot protect the one thing under attack when the
arguments are platform UUIDs: the human's reading of a model-authored sentence." A
40-line YAML fragment is a worse version of that sentence, not a better one.

### The seed already in the tree

`connectors/k8s-scale`'s own docstring records the distinction this ADR generalizes:

> `replicas` is a caller parameter here because it is the verb's argument rather than
> a channel into an arbitrary patch

A caller-supplied *value at a fixed path* is safe. A caller-supplied *path* is not.
RBAC cannot express that difference. A connector can.

## Decision

**A write connector publishes one tool per resource kind. What that tool may change
is a builder-declared set of field paths with value constraints, enforced inside the
connector. The agent composes a change within those paths; it can neither author a
path nor widen the set.**

1. **The unit of authorization is a triple**, not a tool name: `(resource kind, field
   path, value constraint)`. `restart` and `scale` become two entries against one
   tool rather than two connectors:

   ```
   apps/Deployment  /spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt
                    value: server-generated timestamp, no caller input
   apps/Deployment  /spec/replicas
                    value: integer, 1..N, N declared
   ```

2. **Every triple is classified `deny` | `approval-required` | `allow`, and an
   operation matching no triple is denied.** This is the tri-state the #1886 review
   asked for, given a subject specific enough to attach to: a tool name cannot carry
   three classifications, a field path can.

3. **The connector composes the patch.** The agent supplies values for declared paths;
   a caller-supplied patch body is never forwarded. The invariant that makes today's
   connector the strongest ceiling is preserved verbatim -- it is only stated as data
   instead of hard-coded per verb.

4. **One input renders the policy and the RBAC ceiling**, extending the principle
   PR #1923 establishes for the write allowlist: the Role's `resourceNames` and the
   connector's permitted targets come from the same declaration, so they cannot
   disagree.

5. **Gates key on the triple, not only the tool name.** Today
   [`runner/src/curie_runner/approval.py`](../../runner/src/curie_runner/approval.py)'s
   `build_can_use_tool` arms a gate by exact match on the published tool name. With one
   tool covering several operations, tool-name gating becomes all-or-nothing and
   [ADR-0050](0050-declared-approval-policy-is-armed-exactly-or-not-at-all.md)'s
   "armed exactly" guarantee stops meaning anything useful. The gate vocabulary must
   name the operation.

6. **The ADR-0117 report is derived from the patch**, not written per verb: the prior
   value at each declared path is read before and after, which is what both existing
   connectors already do by hand.

## Alternatives considered

**One connector tool per verb (the status quo).** Rejected on measured cost: 580
lines for two operations, near-duplicate, plus an image, a gate and an allowlist per
verb -- and a second verb that is merged and still undeployable because its image was
never published. The cost is linear in verbs and the duplication is where the drift
lives.

**A general write passthrough controlled by the agent.** Rejected. RBAC has no field
granularity, so the effective ceiling becomes arbitrary code execution; the agent
reads attacker-writable text cluster-wide by design; and an approval gate over a
model-authored patch body is the failure Draft ADR-0125 (PR #1934) records rather than a mitigation.
The attraction is real and the cost is the entire safety argument.

**Passthrough plus approval on every call.** Rejected as a variant of the above. It
does not add a check, it moves the check to the least reliable reader in the loop.

**RBAC subresources only.** `deployments/scale` is a real subresource, so scale
genuinely can be narrowed by RBAC alone -- which is why it is tempting to answer the
whole question that way. Rejected as insufficient: restart has no subresource, and
neither do most verbs anyone will ask for next. It solves the one case that did not
need solving.

**Platform-owned policy instead of builder-owned.** Not rejected -- deferred. See
below.

## Consequences

- **A new verb becomes a policy entry.** No new image, no new connector, no new
  allowlist to keep in sync. This is the point.
- **The policy evaluator is real work**: strategic-merge versus JSON-patch semantics,
  path matching including escaped path segments, and value validation. It is paid once
  rather than per verb, and it is the load-bearing component -- a bug in it is a
  ceiling failure, so it needs the test attention the per-verb patch bodies currently
  get for free by being constant.
- **`k8s-write` and `k8s-scale` collapse into one connector**, and `k8s-scale`'s
  never-published image stops being a blocker for the second verb.
- **Two temporary guards get deleted, not extended.** In PR #1886,
  `scripts/check-write-path-gated.py` compares a connector's `readOnlyHint` tools
  against declared gates from source, and `scripts/assert-gates-are-live-tools.py`
  does the same against a running connector. Both
  exist because a gate and a connector were two edits that got made separately. Both
  reason about tool names, so both are superseded by a policy that makes the pairing
  unrepresentable.
- **The gate vocabulary changes**, which is a real cost against ADR-0050 and needs
  that ADR read before this one is accepted.
- **`readOnlyHint` stops being load-bearing.** It is an agent-facing annotation, not a
  boundary; with a policy, the classification comes from the policy and the annotation
  goes back to being a hint.

## The one decision this ADR does not make

**Whether an agent may change its own policy. It may not**, and that is deliberate:
the whole decision above is worthless if the constrained party can edit the
constraint. How a policy change *is* made -- an operator edit, a gated publication, a
control-plane screen -- belongs to Draft ADR-0125 (PR #1934), which already proposes that an agent's authority ends at rendering and that execution
requires the platform key plus a server-resolved operator.

**Whether the policy is owned by the bundle or by the platform.** This ADR says
builder-owned, matching where `approvalPolicy` and `connectors.yaml` already live, and
that is the smaller change. A platform-owned policy -- an operator constraining every
bundle on the cluster regardless of what its author declared -- is a stronger and
possibly better answer, and it is a separate decision with a separate blast radius.

## Out of scope

- Non-Kubernetes connectors. The mechanism is not Kubernetes-specific, but the only
  measured evidence here is Kubernetes and the ADR claims no more than it measured.
- Platform upgrade, and an agent upgrading its own version. Those are authority
  questions about the platform rather than about a connector's write surface.
- Read authorization. The read ceiling is a ClusterRole whose blast radius is decided
  by which namespace its subject occupies, which no tool-surface policy can see.
