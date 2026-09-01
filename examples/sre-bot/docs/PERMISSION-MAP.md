# Write path permission map

Every write this bot can perform, what each one actually grants, and which layer
decides it. Written because "give it write access" is not a decision — it is the
absence of one, and the interesting question is always *which* write, permitted
by *whom*, bounded by *what*.

## Two axes, kept apart on purpose

Most confusion about agent write access comes from collapsing these into one
word, "ceiling":

- **Capability surface** — what a connector *can express* against an external
  system. A parameter list, a constant request body, a scoped credential,
  `resourceNames`. This bounds what is reachable at all.
- **Authorization decision** — whether this agent *may call* a published tool,
  and under what conditions. Denied, approval-required, or allowed. This is the
  builder's decision, held in agent config, over the connector's published tool
  surface.

They are not substitutes. A narrow capability surface with no authorization
decision is an ungated write — that is exactly the incident behind
`scripts/check-write-path-gated.py`. An authorization decision over a
capability surface wide enough to do something else is a gate on the wrong
thing. Every entry below is read on both axes.

A connector publishes capabilities against an external system. A skill owns
deterministic workflow logic and sequencing. Neither may widen the builder's
authorization: a skill that could grant itself a tool would make the config
advisory.

---

## 1. `restart_deployment` — rolling a named Deployment

| | |
|---|---|
| Tool | `mcp__k8s-write__restart_deployment(namespace, name)` |
| Kubernetes verbs | `get`, `patch` on `apps/deployments` |
| Scoped by | `resourceNames`, one namespace |
| Authorization | `approvalPolicy` gate — a human approves each call |
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
separates them. This is the fact that makes the credential's breadth invisible
if you read only the verb list.

### Capability surface

The tool takes `namespace` and `name` and nothing else; the patch body is a
constant in the source. There is no parameter through which a caller reaches an
image, a command, or an env var. `resourceNames` bounds which workloads, and the
connector's own allowlist states that bound a second time so an edit to one is
visible against the other (`scripts/check-write-path-gated.py` compares them).

This is defense in depth and it is worth having: the agent is prompt-injectable
and this process is not, so a narrow surface limits what a compromised *agent*
can reach. What it is **not** is the authorization decision. A connector that
publishes exactly one narrow write tool still writes when called, and nothing in
the connector decides whether this agent may call it.

### Authorization decision

The `approvalPolicy` gate, which is the builder's, not the connector's: a human
approves each call before it executes. Verified end to end on a real install —
approval produced the restart (`restartedAt` and `metadata.generation` both
advanced), rejection left both unchanged, and a target outside the allowlist was
refused by the connector *and* independently by RBAC.

Two limits to state rather than imply:

- The gate is a control on the **agent**, not on the **credential**. If the
  credential leaks, the gate is irrelevant. The Role is therefore scoped as
  though the gate did not exist.
- Curie's posture today is approval-required plus **allow-by-omission**. A tool
  no gate names is callable. So "which tools are gated" is the whole policy, and
  a forgotten gate is a silent grant rather than a refusal. See the prerequisite
  in entry 5.

### Deliberately not granted

- `pods delete` — a restart with worse failure modes: no surge, no rollback, no
  record on the Deployment. `rollout restart` covers the same intent safely.
- Anything cluster-scoped, anything in `kube-system`, any `create` or `delete`.

---

## 2. `scale_deployment` — setting a named Deployment's replica count

| | |
|---|---|
| Tool | `mcp__k8s-scale__scale_deployment(namespace, name, replicas)` |
| Kubernetes verbs | `get`, `patch` on `apps/deployments/scale` |
| Scoped by | `resourceNames`, one namespace, plus `K8S_SCALE_ALLOWLIST` and `K8S_SCALE_MAX_REPLICAS` |
| Authorization | `approvalPolicy` gate — a human approves each call |
| Status | Implemented, ships with its credential absent |

### What that grant actually permits

This is the one entry in this document where **RBAC is the strong constraint and
the connector is defense in depth**, rather than the other way round. Entry 1
has to enforce its own narrowness in Python because `patch` on `deployments` is
the same grant as `set image`, `set env`, and replacing the container command --
Kubernetes cannot tell those apart. Scaling can be told apart: `scale` is its own
subresource, so `patch` on `apps/deployments/scale` grants the replica count and
nothing else. An attempt to change an image with this credential is refused by
the API server, not by a file in this repository.

What it still permits is `--replicas=0`, which is an outage. The grant does not
distinguish that from any other number, so the things that do are the connector's
allowlist, its `K8S_SCALE_MAX_REPLICAS` ceiling, and the gate.

### Capability surface

`replicas` is a caller parameter here, which entry 1 deliberately does not have.
That is safe for the reason above and only for that reason: the parameter is the
verb's argument, not a channel into an arbitrary patch body, because the
subresource cannot carry one.

### Authorization decision

The gate `mcp__k8s-scale__scale_deployment`, on the same route as entry 1.

### Reversible, and it says what to put back

Unlike a restart, a scale can be undone: the replica count in force immediately
before the patch is enough to restore it. The tool reads that count on the way
past and returns it, and **refuses to write if it cannot read one** -- an action
that happened without a recorded prior state leaves the platform holding a record
it cannot act on (ADR-0117). This is why the reply is JSON rather than prose.

### Deliberately not granted

`patch` on `deployments` itself. Granting it to this identity "for tidiness"
would throw away the subresource ceiling that is this entry's whole argument.

---

## 3. `upgrade_self` — starting this bot's own version upgrade

| | |
|---|---|
| Tool | `mcp__self-upgrade__upgrade_self()` — no arguments |
| Kubernetes verbs | `get` on `batch/cronjobs`; `create`, `list` on `batch/jobs` |
| Scoped by | `resourceNames` on the CronJob; **nothing scopes the create** — see below |
| Authorization | `approvalPolicy` gate — a human approves each call |
| Status | Implemented, ships with its credential absent. Proven end to end on a live cluster 2026-08-28 |

This upgrades the **bot's own bundle version** -- a new agent version built from
the repository. It is not entry 4, which upgrades the Curie release underneath
it. They are different operations with different blast radii, and the shared word
"upgrade" is the only thing they have in common.

### The grant RBAC cannot narrow

`create` on `jobs` is namespace-wide and cannot be otherwise. `resourceNames`
matches against the name of an existing object, and a create has no name yet, so
there is no RBAC expression for "may create only this Job". A credential with
this Role can in principle create a Job running any image with any command in the
release's namespace -- the namespace holding the platform's API key.

That is the same shape as entry 1's `patch` on `deployments`, and it gets the
same answer: the ceiling is enforced in the connector, which exposes one tool
that **takes no arguments at all** and posts the named CronJob's
`jobTemplate.spec` verbatim. There is no field a caller can reach, so there is
nothing to validate and nothing to escape.

Read honestly, what remains is a credential whose blast radius is the release
namespace if the token escapes the connector pod. It does not leave the pod, for
the same reason the read connector drops `configuration_view`: the sandbox learns
a URL and never holds a credential.

### The service-account escalation RBAC also cannot prevent

When this Role is installed beside `manifests/platform-upgrade-role.yaml`, a
leaked `sre-bot-upgrader` token can create a Job with
`spec.serviceAccountName: curie-platform-upgrader`. Kubernetes RBAC authorizes
the Job create but does not restrict which service account the created Pod runs
as, so that Job inherits the platform upgrader's namespace-wide permissions.
The connector cannot make that request -- its one tool posts an operator-written
CronJob template verbatim -- but a holder of the leaked token bypasses the
connector entirely.

This is a disclosure, not a permission change. Mitigate it with an admission
policy that constrains service-account choice for the connector identity, by
placing the wider identity in a separate namespace, or by avoiding a long-lived
connector token. If none is acceptable, do not install this connector.

### Why this is not the abstraction entry 5 rejects

Entry 4 rejects `upgrade_release(target_version)` -- a connector validating a
version against a list it holds itself -- because that makes the connector the
policy holder, in a process the builder cannot edit or inspect per-agent.

This tool holds no policy. It has no version list, no sequencing, and no
decision: which repository, which branch, which image and which command all come
from a CronJob an operator wrote and can read. The connector's entire
contribution is "start that, now, if nothing like it is already running". Move
any of those choices into the connector and entry 5's objection would apply here
too.

### Why the bot does not simply do the upgrade

Creating an agent version needs the platform API key, and every `/agents/**`
route requires it. The sandbox holds a per-turn `state`-scoped token and nothing
else, deliberately. Handing the sandbox the platform key so that "upgrade
yourself" could work in one step would trade that property for a convenience, and
the property is what makes a successful prompt injection unable to walk away with
a credential. So the key stays in the Job and the bot only presses the button.

### Not reversible, and the reply says so

There is no undo tool. `prior` is null on every path including the successful
one, so the platform never records a snapshot it cannot act on. Restoring the
previous version is an operator action with the platform API key, named in the
reply rather than implied.

Starting the Job is also not finishing it. The reply carries the Job's name and
says so in as many words, because the agent reports from that text and "started"
read as "succeeded" is the failure this wording exists to prevent.

### Deliberately not granted

`delete` on `jobs`. Cleaning up finished Jobs is the CronJob's
`successfulJobsHistoryLimit`, and `delete` would let a prompt-injected agent
erase the evidence of what it started.

---

## 4. `upgrade_platform` — moving the release to the newest published version

| | |
|---|---|
| Tool | `mcp__self-upgrade__upgrade_platform()` — no arguments |
| Kubernetes verbs | `get` on `batch/cronjobs`; `create`, `list` on `batch/jobs` |
| The JOB's verbs | namespace-wide write across the kinds the chart owns — see `manifests/platform-upgrade-role.yaml` |
| Authorization | `approvalPolicy` gate — a human approves each call |
| Status | Implemented, ships with its CronJob and identity absent |

This is entry 5's problem solved by giving up the thing entry 5 wanted most: a
**named version**. It upgrades to whatever the project published last, and there
is no way to ask it for a particular release.

That trade is what makes it shippable now. Entry 5's design needs a
general-purpose Helm tool surface, which cannot be safely classified while the
platform's live behaviour is allow-by-omission (curie#2119). One no-argument
verb has no surface to classify.

### The grant, and where it actually lives

The bot's own grant is the same as entry 3's: start a Job, read its name. The
credential that can rewrite the release belongs to the **Job**, not to the bot
and not to a connector — it exists for the ninety seconds the upgrade runs and
the sandbox never sees it.

Read `manifests/platform-upgrade-role.yaml` before installing this. Its rules are
namespace-admin in all but name, and the file says so and enumerates them rather
than burying it. What bounds it is lifetime, an operator-written script the bot
cannot edit, and the namespace; what does NOT bound it is the gate or the bot's
good behaviour, which govern only who starts the Job.

### Why the version is not a parameter

A version argument is caller input, and the caller is a language model. Entry 5
is right that the allowlist of acceptable versions is builder config rather than
connector state — so until there is a builder-owned place to put it, "newest
published" is the only target this can honestly offer.

In practice that is usually the question being asked. The request that produced
this entry named a specific version, and that version was already two releases
stale; what the person wanted was "get us current".

### Deliberately not granted

No rollback verb. `helm rollback` restores objects but not the database —
migrations run as an init container and rollback does not undo them — so for a
version pair that migrated, recovery is restore-from-backup by an operator. A
rollback tool here would read as an undo that this cannot perform.

---

## 5. Upgrading Curie by naming a version — a workflow, not a tool

**PROPOSED. Not implemented.** Superseded as a weekly requirement — issue #1857
asks for "rollback or recovery behavior", not a named-version upgrade — so this
entry is the design record for when it is wanted, not a plan for this week.

### Why the entry-1 shape does not extend here

Entry 1 is narrow because the *credential* is narrow. That is not available
here. One Helm operation updates essentially every namespaced object the release
owns — Deployments, StatefulSets, Services, ServiceAccounts, Roles,
RoleBindings, Jobs, ConfigMaps, the release Secret — plus schema migrations and
Helm's own release state. The exact inventory moves with which components are
enabled, so it is worth re-deriving rather than quoting:

```
helm template curie charts/curie | grep -c '^kind:'
```

Two corrections to what an earlier draft of this file asserted, both of which
change the argument rather than decorate it:

- **The `agents.x-k8s.io` CRDs are not in this chart.** They are installed
  outside it, so a release upgrade does not carry them. A version pair that needs
  a newer CRD needs a separate, cluster-scoped step — which is a separate
  authorization question, and a stronger one, not a line item inside this tool's
  breadth.
- **Storage is the part an upgrade cannot change.** The four PVCs come from
  StatefulSet `volumeClaimTemplates`, which Kubernetes treats as immutable on
  update. That is reassuring for the data and is exactly why recovery is not
  symmetric with upgrade: see the migration finding under "measured".

There is still no RBAC expression for "may upgrade Curie". The nearest honest
expression is **namespace-admin over the release's namespace**, which is an
unbounded credential in every sense the acceptance criteria exclude.

The tempting move is to absorb that breadth into a bespoke connector: one tool,
`upgrade_release(target_version)`, validating the version against a list it
holds itself. **That is the wrong abstraction** and this document previously
proposed it. It makes the connector the policy holder — the version allowlist,
the sequencing, and the decision to proceed all live in a process the builder
does not edit and cannot inspect per-agent. Every future agent wanting a
different upgrade policy needs a different connector image.

### The shape that keeps the boundaries where they belong

**Connectors publish external capabilities.** A Helm connector publishes what
Helm does — `helm_upgrade(release, chart, version, values_ref)`,
`helm_history(release)`, `helm_rollback(release, revision)`,
`helm_status(release)` — and a Kubernetes read connector publishes the reads used
for verification. These are ordinary tools against an external system, carrying
no opinion about which versions are acceptable or in what order to call them.

**The skill owns the workflow.** Preflight, change record, execution order,
health verification, and recovery instructions are deterministic sequencing, so
they belong in the skill where they are readable, reviewable, and versioned with
the bundle:

1. Preflight — record chart and app version, Helm revision, image digests, pod
   inventory, schema revision; confirm the target's images are pullable;
   back up before anything that migrates.
2. Change record — the Helm revision history *is* the record, with the caveat in
   "measured" below.
3. Execute — the connector's `helm_upgrade`, one call.
4. Verify — pods ready is necessary and not sufficient; verify a real turn
   completes, because that is what the upgrade was for.
5. Recover — named-revision rollback, or restore-from-backup where a migration
   is not reversible.

**Agent config makes the authorization decision.** Per published tool:

| Tool | Classification | Why |
|---|---|---|
| `helm_upgrade` | approval-required | the operation with the breadth |
| `helm_rollback` | approval-required | equally broad; also a recovery path, so denying it strands the bot mid-incident |
| `helm_history`, `helm_status`, reads | allowed | needed for preflight and verification, no mutation |
| `helm_uninstall`, `helm_install` | denied | not in this workflow's scope at any approval level |

The version allowlist is builder config too, not connector state — it is an
authorization question ("may this agent move to that version"), not a capability
question ("can Helm express it").

**Defense in depth, still worth having, still not the policy.** A scoped
credential, `resourceNames` where they apply, connector-side validation, and
narrow parameter surfaces all reduce what a compromised agent or connector
reaches. None of them decides whether this agent may upgrade, and none should be
read as if it had.

### Prerequisite, stated rather than worked around

Curie today has approval-required gates and **allow-by-omission**: a published
tool no gate names is callable, and nothing reports the omission.

The builder-owned deny / approval-required / allow contract now EXISTS as a
bundle format -- `toolPolicy` in `plugin-format`, with class precedence
(`deny` > `approvalRequired` > `allow`), a versioned `enforcement`
discriminator, and a docstring stating that a tool no collection matches is
denied. What is missing is the wiring: nothing outside that package calls
`classify_tool`, the runner has no reference to it, and the only bundle
declaring one is a test fixture (curie#2119).

So the prerequisite is half built, and the half that is missing is the half
that binds. Until something applies the classification where tools are offered
to the model, the live behaviour is still allow-by-omission and the paragraph
below still holds -- but the remaining work is wiring an existing contract,
not designing one.

For entry 1 that gap is survivable, because a repository lint can compare a
small set of source-carried connectors against the declared gates. For this
workflow it is not: the tool surface is a general-purpose Helm connector, where
"every tool not explicitly classified is callable" means one added tool in a
future connector version silently widens the agent. **The tri-state contract is
a prerequisite for shipping this workflow**, and the right order is to build it
in the platform rather than to route around it with a policy-bearing connector.

### Measured, so these are no longer open questions

Run on a local install, curie `0.7.0` → `0.7.1` → `0.7.0` → `0.7.1`:

- **Does a turn survive the upgrade restarting the worker running it?** Yes,
  when approval comes first. Approval-suspended sessions are resumed by the
  **API**, not the worker (`approvalSweepIntervalSeconds`, 30s default), and the
  API is a separate process from the worker being replaced. Demonstrated in
  miniature: the bot restarted `curie-api` — the component hosting that sweeper —
  through the gated write path, and its own turn still completed. An in-flight
  turn *not* suspended on an approval is a different story: a queue entry held by
  a dead worker's consumer sat idle 808s and 428s before a new consumer picked it
  up with delivery-count 2. So the ordering is load-bearing — approve, then
  execute, then resume — and the skill's sequencing must not invert it.
- **What does rollback mean?** For this version pair, a Helm operation: schema
  revision was `0027` before and after, so nothing migrated. In general it is
  not, and the reason is structural — migrations run as an api initContainer
  (`alembic upgrade head`) and `helm rollback` does not run `alembic downgrade`.
  Where a version pair migrates, recovery is restore-from-backup and the bot's
  role in it should be written as "cannot".
- **Is the Helm history a usable change record?** Not on its own. Every
  `cluster up` against a cluster with no `runsc` records a *failed* revision
  before its successful one, so history alternates and a bare `helm rollback`
  targets a failed revision (curie#1899). Rollback must name a revision, and the
  runbook step above says so.
- Timings, for scale rather than as a guarantee: upgrade 34s, rollback 35s, both
  preserving the recorded model credential.

### Still open

1. **Which versions belong on the allowlist, and who edits it?** "Latest" is not
   an allowlist. A pinned list is safe and goes stale; a range is convenient and
   is how you upgrade into something untested. This is builder config, so the
   answer is a policy owner, not a mechanism.
2. **Where does the Helm connector's credential live, and is one shared across
   clusters?** Connector secrets currently have no cluster scope, so a deploy can
   inject another cluster's credential (curie#1913). A broad upgrade credential
   makes that considerably less academic than it was for entry 1.
