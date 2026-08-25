# Write path permission map

Every write this bot can perform, what each one actually grants, and where its
ceiling really is. Written because "give it write access" is not a decision —
it is the absence of one, and the interesting question is always *which* write,
bounded by *what*.

Two entries. The first ships and is off by default. The second is proposed and
is a different kind of problem, which is most of why this file exists.

---

## 1. `restart_deployment` — rolling a named Deployment

| | |
|---|---|
| Tool | `mcp__k8s-write__restart_deployment(namespace, name)` |
| Kubernetes verbs | `get`, `patch` on `apps/deployments` |
| Scoped by | `resourceNames`, one namespace |
| Gate | `approvalPolicy`, human approves each call |
| Status | Implemented, ships commented out |

### What that grant actually permits

`kubectl rollout restart` is not a distinct permission. It is a PATCH of the pod
template that sets an annotation, so `patch` on `deployments` is the same grant
as:

```
kubectl set image deploy/<name> app=something-else
kubectl set env   deploy/<name> ANTHROPIC_API_KEY=...
kubectl patch     deploy/<name> -p '{"spec":{"template":{"spec":{"containers":[{"command":[...]}]}}}}'
```

**A grant that permits a restart permits replacing what runs.** Kubernetes RBAC
cannot express "patch only this one field", so nothing at the RBAC layer
separates them.

### Where the ceiling really is

Ranked by strength, because the weakest one is the easiest to mistake for the
whole answer:

1. **The connector.** The tool takes `namespace` and `name` and nothing else, and
   the patch body is a constant in the source. There is no parameter through
   which a caller reaches an image, a command, or an env var. The agent is
   prompt-injectable; this process is not.
2. **`resourceNames`.** Bounds which workloads, so even a compromised connector
   reaches only what an operator named. The only narrowing RBAC offers here.
3. **The allowlist.** The same bound stated again in the connector's own config,
   because a ceiling written in one place moves when someone edits the other.
   These two must agree; `scripts/check-write-path-gated.py` enforces that.
4. **The approval gate.** Stops a call before it executes — but it is a control
   on the *agent*, not on the *credential*. If the credential leaks the gate is
   irrelevant, so the Role is scoped as though the gate did not exist.

### Deliberately not granted

- `pods delete` — a restart with worse failure modes: no surge, no rollback, no
  record on the Deployment. `rollout restart` covers the same intent safely.
- `deployments scale` — a separate blast radius; scale-to-zero is an outage.
  Add it with its own gate when someone asks, not pre-emptively.
- Anything cluster-scoped, anything in `kube-system`, any `create` or `delete`.

---

## 2. `upgrade_release` — moving Curie from one version to the next

**PROPOSED. Not implemented. The design question below is the reason this file
exists rather than a Role.**

### Why this one does not fit the pattern above

The first entry is narrow because the *credential* is narrow. That is not
available here. Upgrading a Curie release touches, in one operation:

```
Deployments 6   StatefulSets 4   Services 7   PVCs 4
ServiceAccounts/Roles/RoleBindings 3 each      Jobs 3
ConfigMaps 2    Secrets 1
+ 4 cluster-scoped CRDs (agents.x-k8s.io)
+ database schema migrations
+ Helm release state
```

There is no RBAC expression for "may upgrade Curie". The nearest honest
expression is **namespace-admin plus CRD write**, which is an unbounded
credential in every sense the acceptance criteria are trying to exclude.

So the ceiling cannot come from RBAC. It has to come from somewhere else, and
that choice should be explicit rather than discovered later.

### The proposal

Move the boundary entirely into the connector, and say so plainly rather than
implying RBAC is doing work it cannot do.

| | |
|---|---|
| Tool | `upgrade_release(target_version)` — one parameter |
| Parameter | Validated against an allowlist of versions, not free-form |
| Credential | Broad within one namespace. **This is the honest part.** |
| Gate | `approvalPolicy`, and the card must show from-version and to-version |
| Ceiling | The connector, which constructs the operation and accepts no other input |

The credential being broad is not a compromise to hide; it is a fact about what
an upgrade is. What bounds the bot is that the only thing it can *say* is "go to
this version", chosen from a list, with a human approving the specific pair.

### Open questions, which need answering before anything is built

1. **How does the connector perform the upgrade?** It talks to the Kubernetes
   API today. An upgrade is a Helm operation. Either the connector ships the
   `curie`/`helm` binary and shells out with a constructed argv, or it applies
   rendered manifests directly. The first is closer to how upgrades are actually
   done and easier to verify; the second avoids a binary in the image but
   reimplements release bookkeeping, which is a bad trade.

2. **What does rollback mean, and can the bot do it?** The acceptance criteria
   ask for "rollback or recovery posture demonstrated". `helm rollback` is
   another broad operation, and a schema migration may not be reversible — 0.7.0
   moved `agents.slack_channel` into its own table. Rollback may be *restore
   from backup*, not a Helm verb, and if so the bot's role in it should be
   stated as "cannot" rather than left ambiguous.

3. **What happens when the bot upgrades the platform it runs on?** The three
   Deployments in entry 1 are Curie's own. An upgrade replaces the worker
   mid-turn, including the turn performing the upgrade. The approval resumes
   through the worker being restarted. Whether that survives is a question to
   answer by testing, not by reasoning — and if it does not, the upgrade may
   need to be initiated from outside the bot even though the bot requests it.

4. **Which versions belong on the allowlist, and who edits it?** "Latest" is not
   an allowlist. A pinned list is safe and goes stale; a range is convenient and
   is how you upgrade into something untested.

### Status

Not built. Questions 1 and 3 in particular decide whether the shape above is
right at all, and question 3 is answerable in an afternoon on a staging install.
