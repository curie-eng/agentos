# SRE triage bot

A production-health assistant that lives in Slack. Ask it questions in plain
English; it reads your Kubernetes cluster and answers in plain English. Nobody
asking needs to know PromQL, LogQL, or which datasource holds what.

```
@sre-bot is anything broken right now?
@sre-bot did anything get OOMKilled today?
@sre-bot why is the api slow?
@sre-bot is the core rollout stuck?
@sre-bot which pods are pending?
```

What you get back: a one-line verdict first ("Nothing looks broken." / "Yes --
`api` is throwing 500s"), then the evidence, then a link so you can look
yourself.

**Read-only operation remains the default.** The Kubernetes read connector is
on. The one write connector and its exact approval gate ship declared together,
so the tool cannot be enabled without its gate. On a genuinely untouched
bring-up, `cluster deploy` refuses on the unbuilt `connectors.lock.yaml` before
it ever reaches the secret check; once built, the still-missing separate
`K8S_WRITE_KUBECONFIG` refuses just as cleanly before any connector starts,
instead of silently acquiring write access.

Completing the deliberate setup gives the bot exactly one thing it can change:
rolling one allowlisted Deployment behind a human approval card. See
[Level up: the gated write path](#level-up-the-gated-write-path).

This is the repo's most complete example bundle. It is a real bot, generalised:
a skill, four declared connectors (Kubernetes read, gated write, Grafana and
Tempo), the RBAC to stand it up, connector source and tests, deploy targets, and
a falsifiable eval suite.

---

## The security shape

Four properties, and they are the reason the bundle looks the way it does.

**The sandbox never holds a credential.** The agent is prompt-injectable by
construction -- it reads logs, alert text and dashboard titles that anyone can
write into. So the credential lives in the connector process, and the sandbox
only ever learns a URL. A successful injection can call the read tools the
connector already allows; it cannot walk away with the token and post it into a
channel.

**Read-only is enforced server-side, and tool filtering is defense in depth.**
The RBAC on the mounted kubeconfig is the real boundary: a write is refused by
the API server whatever the agent tries. `--read-only` and `--disable-destructive`
sit on top of that, and they earn their place for a different reason -- the
agent cannot be talked into calling a tool it cannot see.

**"Read-only" is not the same as "safe".** `configuration_view` returns the
kubeconfig as YAML, bearer token in cleartext, and it is annotated
`readOnlyHint=true` -- because reading a credential IS a read. It shipped in this
connector for four days behind a verified "all 14 tools are read-only" check.
`--toolsets core` is what removes it. Audit what a read tool RETURNS, not just
its annotation.

**The write path is four layers, and RBAC is only the third.** Listed strongest
first, because the weakest is the one people assume is doing the work:

| # | Layer | What it actually stops |
|---|---|---|
| 1 | **The connector's tool surface** | `restart_deployment` takes a namespace and a name. There is no parameter through which a caller reaches an image, a command, an env var or a replica count, and the patch body is a constant in `connectors/k8s-write/server.py` with only a timestamp filled in. This is the real ceiling. |
| 2 | **The allowlist inside the connector** (`K8S_WRITE_ALLOWLIST`) | Checked before the request is built. Empty means the connector refuses to START -- a missing config fails closed rather than permitting everything. |
| 3 | **RBAC** (`manifests/write-role.yaml`) | `get`+`patch` on named Deployments in one namespace. Bounds a compromised connector. |
| 4 | **The approval gate** | A control on the agent, not on the credential. If the credential leaked, the gate would be irrelevant. |

**Why RBAC is only third.** `kubectl rollout restart` is not a distinct
permission -- it is a PATCH of the pod template. So `patch` on deployments is the
SAME grant as `set image`, `set env`, and replacing the container command.
Kubernetes cannot express "patch only this annotation". Anyone reading the Role
and thinking "restart is harmless, let me add a namespace" is widening arbitrary
code execution across it.

**Order matters: scope layers 1 to 3 as if the gate were absent.**

---

## Quick deploy

On a clean Kubernetes cluster, run one command:

```bash
curie example sre-bot install --observability
```

This installs the bot and its self referential observability stack with no
values files. With no targeting flags, Curie lands as release `curie` in
namespace `curie`, and the retained Grafana/Loki/Alloy/Tempo/Prometheus stack
lands in namespace `observability`. To install beside an existing release, pass
the same identity the rest of the cluster CLI uses:

```bash
curie example sre-bot install --observability \
  --namespace soak \
  --release soak \
  --observability-namespace soak-obs
```

`--namespace` also reads `CURIE_NAMESPACE`. The installer threads those three
values through every Helm, kubectl, manifest, secret-discovery, connector, and
deploy step, including the embedded `read-access.yaml` and observability
service DNS. The installer makes a read only runtime copy of the checked in
bundle by removing the write connector and its matching approval gate together.
The declared write path remains in the source bundle for the explicit Level up
build and deploy flow below.

### One source of capacity truth

This Prometheus is the bot's own evidence, not a cluster-wide scrape platform.
It discovers annotated targets **only in its own namespace** -- the one
`--observability-namespace` names -- which is where the installer puts
kube-state-metrics and the node exporter, and it stamps every sample it scrapes
with `curie_source="curie-sre-bot"`.

That boundary is why installing on a cluster that already runs monitoring is
safe. The stock chart keeps any `prometheus.io/scrape` target in any namespace,
so beside an existing stack it would ingest a second kube-state-metrics and a
second node exporter: one restart, two series, identical workload labels, and a
bot confidently reporting double the restarts and double the estate. Scoping
discovery removes the second source; the stamp says which install an answer came
from, which is what keeps it legible if this store is ever federated into or
read beside another one.

Note what the stamp does **not** cover. Prometheus synthesizes `up`,
`scrape_duration_seconds` and the rest of the per-scrape series after metric
relabeling, so those carry no `curie_source` -- qualify them by `job`. And the
scope is a namespace, not a release: put a second monitoring stack in *this*
namespace and both kube-state-metrics instances are back in scope, stamped
identically. Give this stack a namespace of its own.

What you give up is real, and it is more than application metrics. Outside this
namespace nothing annotated is collected here any more: not app pods, not
third-party Service exporters such as an ingress controller or a database
exporter, and not `prometheus.io/probe` blackbox targets. So cluster-wide
service health, error-rate and probe evidence that used to arrive through those
annotations is gone from *this* Prometheus -- the bot still sees every
namespace's objects through kube-state-metrics and the Kubernetes API, and every
namespace's logs through Loki, but not those exporters' own metrics. If you want
a shared, cluster-wide scrape platform, run one and point Grafana at it rather
than widening this one: the moment this Prometheus holds two sources for the
same object, every unqualified capacity answer the bot gives is wrong in a way
nothing in the pipeline will flag. The node-level jobs are deliberately left
cluster-wide -- they resolve one target per Node through the API server, so they
cannot double count, and the bot would otherwise be blind to every kubelet.

### Retained Curie metrics and reliability alerts

The base chart still ships `nop/metrics`. The installer overlay
`observability/curie-values.yaml` appends `prometheusremotewrite/soak` to that
pipeline and opens Collector metrics ingress for the Prometheus server. The
Prometheus overlay enables the remote-write receiver and loads the
`curie-reliability` alert group. Alertmanager stays off: alerts fire inside
Prometheus. This does not send notifications to people.

Source files and a green `helm template` are the locally rendered class.
A disposable install that can query series and show alert firing plus recovery
is runtime-tested. Neither class means the permanent soak overlay is actually
deployed. The operator procedure that keeps those classes honest is
[docs/METRICS-ROLLOUT.md](docs/METRICS-ROLLOUT.md).

### Correlate a message, run, delivery, and trace without a private body

Metric labels stay bounded: operation class and outcome only. Run, session,
sandbox, user, and deployment identifiers are correlation attributes on logs
and traces. To diagnose a failed synthetic request:

1. Take the accepted-message timestamp and the W3C `trace_id` from the
   structured log line (not the body).
2. In Tempo, open that `traceId`. Confirm `curie.queue.enqueue`,
   `curie.turn.process`, `curie.runner.rpc`, `agent.run`, and
   `curie.reply.post` on the same trace.
3. In Prometheus, check the matching low-cardinality series
   (`curie_turn_completed_total`, `curie_reply_delivery_total`,
   `curie_runner_rpc_result_total`, `curie_queue_message_age_seconds`).
   Do not add `run_id` or `trace_id` to those queries as label matchers;
   those identities are not metric labels.
4. If the reply is owed, use `curie_completion_outbox` /
   `curie_completion_outbox_age_seconds` rather than reading the run queue
   lag. Missing series is a failure, not a quiet success.

Diagnose without inspecting a private message body or a credential value.
High-cardinality identity belongs in logs and traces, not metric labels.

---

## Talk to it in Slack

`cluster message` is the no-Slack path -- useful for a smoke test, but not what
this bot is for. To wire it into a real workspace:

```bash
export SLACK_APP_TOKEN=xapp-...
export SLACK_BOT_TOKEN=xoxb-...
curie cluster comms --slack

curie example sre-bot install --observability --slack-channel C0EXAMPLE1
```

Then invite the bot to the channel and `@mention` it.

**Bind by channel ID, never by `#name`.** The dispatcher routes by ID, so a name
binds nothing and the deploy still reports success -- the bot simply goes silent
with nothing in any log to explain it. Right-click the channel in Slack -> View
channel details -> the ID is at the bottom, starting with `C`.

---

## Level up: the gated write path

The bot gains exactly one thing it can change: rolling one named Deployment.
Every call pauses the turn and posts an approval card, **and the person who
asked may approve only when their authenticated principal belongs to the
configured approver set**. Requester equality neither grants nor vetoes that
membership; two-person separation requires a distinct policy.

The connector source declaration and the exact
`mcp__k8s-write__restart_deployment` gate already ship together. There is no
connector block to uncomment and no gate snippet to add. The remaining work is
to scope the identity and allowlist, store the separate credential, then build,
bind the approval route, and deploy from the declaration.

Read the four-layer table above before starting, and
[`manifests/write-role.yaml`](manifests/write-role.yaml) before applying it.

**Prerequisite: both kubeconfigs must be available to a manual `cluster
deploy`.** `cluster deploy` refuses unless BOTH `K8S_READONLY_KUBECONFIG` and
`K8S_WRITE_KUBECONFIG` are available -- exported in the environment or stored
with `curie secrets set` -- the bundle declares both `secret_files` entries
regardless of which path you're bringing up. This holds even on an
install where `curie example sre-bot install` already ran: Quick-deploy mints
the read-only kubeconfig and passes it to its own deploy as a one-off secret
override, but never persists it to the host vault, so a later manual `cluster
deploy` still needs `K8S_READONLY_KUBECONFIG` supplied in the environment or
stored yourself. Apply
[`manifests/read-access.yaml`](manifests/read-access.yaml) if it isn't already
applied, then assemble and store `K8S_READONLY_KUBECONFIG` from its
`sre-bot-reader-token` Secret the same way step 2 below builds the write
kubeconfig from `sre-bot-writer-token`.

**1. Scope and create the write identity.**

Edit the namespace and the `resourceNames` list in
[`manifests/write-role.yaml`](manifests/write-role.yaml) first -- one Deployment,
not a list, unless someone has actually asked for the second one. Replace
`<namespace>/<deployment>` under `K8S_WRITE_ALLOWLIST` in
[`connectors.yaml`](connectors.yaml) with the same pair. Stating the ceiling in
two places is deliberate: a ceiling stated once is a ceiling that moves when
someone edits the other place. Then:

```bash
kubectl apply -f examples/sre-bot/manifests/write-role.yaml
```

**2. Assemble and store the write kubeconfig.**

Same shape as the read-only one, from the `sre-bot-writer-token` Secret step 1
minted. This must be a SEPARATE credential -- never the read-only connector's,
which is deliberately unable to write. Use the namespace you set in
`write-role.yaml`:

```bash
NS=<namespace>
CA=$(kubectl -n "$NS" get secret sre-bot-writer-token -o jsonpath='{.data.ca\.crt}')
TOKEN=$(kubectl -n "$NS" get secret sre-bot-writer-token -o jsonpath='{.data.token}' | base64 -d)

export K8S_WRITE_KUBECONFIG="$(cat <<YAML
apiVersion: v1
kind: Config
clusters: [{name: prod, cluster: {server: https://kubernetes.default.svc, certificate-authority-data: $CA}}]
users: [{name: sre-bot-writer, user: {token: $TOKEN}}]
contexts: [{name: prod, context: {cluster: prod, user: sre-bot-writer}}]
current-context: prod
YAML
)"
curie secrets set K8S_WRITE_KUBECONFIG --from-env K8S_WRITE_KUBECONFIG
```

Prove the ceiling before wiring it up. The second and third must fail:

```bash
kubectl auth can-i --as=system:serviceaccount:"$NS":sre-bot-writer \
  patch deployment/<deployment> -n "$NS"                      # yes
kubectl auth can-i --as=system:serviceaccount:"$NS":sre-bot-writer \
  patch deployments -n "$NS"                                  # NO (no resourceName)
kubectl auth can-i --as=system:serviceaccount:"$NS":sre-bot-writer \
  delete deployments -n "$NS"                                 # NO
```

**The credential comes before bring-up, and that order is load-bearing.** The
declared `secret_files` entry has no bundled value. On a genuinely untouched
bring-up -- before `curie build` has ever run -- `cluster deploy` refuses on
the unbuilt `connectors.lock.yaml` before it reaches the secret check at all.
Once built (next step) with `K8S_WRITE_KUBECONFIG` still unstored, the bundle
publishes to the API first and only then does deploy refuse cleanly on the
missing secret, before any connector object is applied. The writer can never
fall back to the read-only connector's credential.

**3. Build the connector image.**

**Needs a buildx driver that supports multi-platform builds.** The declaration
builds both `linux/amd64` and `linux/arm64`, and the stock `docker` driver
refuses that with `Multi-platform build is not supported for the docker
driver`. Unblock it once with `docker buildx create --driver docker-container
--use` (or enable the containerd image store).

```bash
curie build --plugin-dir examples/sre-bot --registry <registry-reference>
```

The declaration builds `connectors/k8s-write` for `linux/amd64` and
`linux/arm64` and writes its resolved digest to `connectors.lock.yaml`. This one
command replaces the old manual image build and push ceremony. The exact
approval gate is already versioned in the plugin manifest. There is no image
line, connector uncomment, or gate edit in the bring-up path.

**4. Bind the route before deploy.**

```bash
curie cluster approvals sre-bot --route-resolution sre-approvals=C0EXAMPLE1
curie cluster approvals sre-bot --list-routes
```

The bundle already declares `sre-approvals`. A deploy whose bundle names a route
with no entry in this agent's `approval_routes` is refused (`422`, or a rejected
push with code `approval_routes.unbound`). Bind first; a bound route no bundle
has declared yet is accepted, so this write is safe on the already-installed
agent. Request-time escalation remains the backstop if a binding is later
removed.

**Route-binding writes and `--gate` are FULL REPLACEMENTS, not additive.** A
write using `--route-resolution`, `--route-approvers`, or `--routes-from`
replaces the whole route map, so name every route you want on every invocation.
Notifications are declared only in the complete `--routes-from` map. Passing one
route to add it silently drops the others.

**5. Deploy the bundle.**

```bash
curie cluster deploy --plugin-dir examples/sre-bot
```

The deploy renders only the digest from `connectors.lock.yaml`.

**6. Request one restart, confirm it as an authorized principal, and verify the rollout.**

In Slack, an authenticated user who belongs to the configured approver set clicks
the card. That may be the requester: requester equality neither grants membership
nor vetoes it. The terminal alternative below uses a subject-bound operator
principal and therefore requires that subject in `approvers.users`.

**Use two shells, and give the asking one a long timeout.** This is the terminal pattern
that actually works, and the reason is worth knowing: on the cluster tier there
is no way to read a resumed reply without Slack. If the approval outlives the
`cluster message` call, the worker completes the resume *best-effort without
delivering the reply*, and the answer is simply lost. So the first shell has to
still be waiting when the approval lands.

Shell 1, which blocks until the approved turn finishes and then prints the reply:

```bash
curie cluster message --timeout-secs 600 --channel C0EXAMPLE1 \
  "Restart the api deployment in the production namespace."
```

Shell 2, while the first is still waiting:

```bash
curie cluster approvals sre-bot --list
curie cluster approvals sre-bot --mint-operator-principal U0EXAMPLE1
export CURIE_APPROVAL_PRINCIPAL_TOKEN=<one-time-output>
curie cluster approvals sre-bot --resolve <approval-id>
```

The terminal principal carries no Slack channel evidence, so bind this route's
`approvers.users` list to `U0EXAMPLE1` before using the terminal path. Use the
authenticated Slack card instead for channel-membership or group-bound routes.
Raise `--timeout-secs` further if a human is doing the deciding.

To reject instead, add `--reject`:

```bash
curie cluster approvals sre-bot --resolve <approval-id> --reject
```

Shell 1's requester gets a no-action reply ("I won't retry it -- no rollout was
triggered, and the deployment is unchanged") and nothing rolls.

The resumed reply must verify with Kubernetes reads: new pods, their ages, and
relevant events. A successful write response proves only that the patch was
accepted. Independently wait for the rollout before calling the exercise done:

```bash
kubectl rollout status deployment/<deployment> -n "$NS"
```

Also request a restart for a different Deployment. The connector must reject it
as outside `K8S_WRITE_ALLOWLIST`; no approval can widen that ceiling.

### Turning it off in a hurry

**Kill the credential, not the pod.** The RoleBinding is the one thing Curie
does not manage and therefore cannot put back:

```bash
kubectl delete rolebinding sre-bot-writer -n <namespace>
```

Seconds, no deploy, and it holds. The tool stays registered and the agent can
still call it, but the API server refuses every patch with a 403 -- so the
failure lands where you want it, at the credential, and the connector reports it
honestly rather than pretending to have rolled something.

Then remove it properly: remove the `k8s-write` declaration in
[`connectors.yaml`](connectors.yaml) and its exact gate in
`.claude-plugin/plugin.json` in the same change, then redeploy. The connector and
tool disappear, while the bundle stays valid because no gate names an absent
connector.

```bash
curie cluster deploy --plugin-dir examples/sre-bot
```

**Scaling the connector to zero is NOT a reliable kill, and this is the trap.**

```bash
# Only safe when the connector reconciler is OFF. Read below first.
kubectl scale deploy -n curie <release>-<agent>-mcp-k8s-write --replicas=0
```

The worker's connector reconciler (`worker.connectorReconciler.enabled`, off by
default in the chart but ON in the git-flow deploy setups that need it)
re-applies every declared connector object on a ~60s loop. The rendered
Deployment declares `replicas: 1`, and the apply is a server-side apply with
`force=True` -- deliberately, so that a field a human took over with `kubectl
edit` comes back. Your `scale --replicas=0` is exactly that kind of field. So the
pod returns within about a minute, the write path is live again, and nothing
tells you: the incident command looked like it worked.

Restore the binding when you are ready by re-applying
[`manifests/write-role.yaml`](manifests/write-role.yaml).

**`curie cluster approvals <agent> --clear` is a trap here and is named so
nobody reaches for it.** It clears the OPERATOR gate; the live gate came from the
bundle's `approvalPolicy`. Clearing it removes nothing, and if the manifest gate
were also absent it would leave the write tool UNGATED -- the opposite of what
you wanted.

---

## Level up: upgrading itself on request

`self-upgrade/cronjob.yaml` already knew how to redeploy this bot from its
repository. What it could not do was happen because someone asked -- it ran on a
schedule, so the answer to "upgrade yourself" was "wait until :17".

The `self-upgrade` connector is the button. One gated tool, `upgrade_self`,
taking **no arguments**: it creates a Job from that CronJob's template, and the
approval card goes to the same channel as every other write.

**The bot does not perform the upgrade, and that is the point.** Creating an
agent version needs the platform API key, and every `/agents/**` route requires
it. The sandbox holds a per-turn `state`-scoped token and nothing else, so a
successful prompt injection cannot walk away with a credential. Handing the
sandbox the platform key to make this feature work would trade that property for
a convenience. The key stays in the Job; the bot only presses the button and
reads the Job's name.

**Read [`manifests/upgrade-role.yaml`](manifests/upgrade-role.yaml) before you
install this.** Its grant is the widest in the bundle: `create` on `jobs` cannot
be narrowed with `resourceNames`, because a create has no name to match. The
ceiling is the connector -- one tool, no arguments, posting the named CronJob's
template verbatim -- not RBAC. That file says so plainly and explains what is
left.

To install it:

1. Apply `manifests/upgrade-role.yaml` (edit its namespace to the one the Curie
   release runs in) and store the resulting kubeconfig as
   `SELF_UPGRADE_KUBECONFIG`.
2. Install the CronJob it starts, following the header of
   [`self-upgrade/cronjob.yaml`](self-upgrade/cronjob.yaml). It ships
   `suspend: true`: the template exists to be started on request. Set
   `suspend: false` to *also* run it on the schedule -- the two are not
   exclusive.

Then ask it in Slack. The bot investigates, calls the tool, says it is
requesting approval, and stops. A human approves; the Job runs; the bot watches
it with the read-only tools and reports what happened.

**There is no undo.** Putting the previous version back is an operator action
with the platform API key, and `SKILL.md` tells the agent to say so rather than
offer a rollback it cannot perform.

**Turning it off in a hurry** works the same way as the write path:

```bash
kubectl delete rolebinding sre-bot-upgrader -n <namespace>
```

---

## Make it yours

**`SKILL.md` is the brain.** Tone, what it checks first, its defaults, its hard
rules. The shipped version is deliberately generic: it tells the agent to
DISCOVER its environment rather than describing one, which makes the bundle
portable and makes the bot slower and more tentative than it needs to be.

The `Your environment` section carries an HTML comment marking where your own
catalogue goes -- datasource UIDs, the namespace map, the metric families worth
reaching for, the alert rules that already exist, and the known noise. The bot
this example came from carries about a hundred lines there. Two rules for
whatever you write:

1. **It is a fast path, never an authority.** Say explicitly that a value which
   does not match what the tools return means the FILE is wrong -- discover the
   real one and say which one you used.
2. **Never describe a tool the bot does not have.** Documenting `search_traces`
   on an install with no tempo connector teaches the bot to claim a capability
   it lacks, which is the failure everything in `Hard rules` exists to prevent.
   The documentation lands in the same change as the connector, never before it.

**The eval suite is the gate, and it is not decorative.**

```bash
cd examples/sre-bot
curie skill up
curie skill eval
curie skill down
```

The `skill` tier hosts nothing, so the Kubernetes connector is not stood up for
you there. Run a copy yourself and hand the runner its URL --
`unhosted_url: ${K8S_MCP_URL}` in `connectors.yaml` is what that variable feeds:

```bash
K8S_MCP_URL=http://host.docker.internal:8765/mcp curie skill up --secret K8S_MCP_URL
```

Without it the three behavioural cases still grade honestly; the capability case
goes red, which is exactly what it is for.

**On the cluster tier the connector is already standing, so all four pass.** The
invocation differs: `cluster eval` grades against the deployed release and takes
`--cases`, never `--plugin-dir` (that flag is `skill eval`'s alone).

```bash
curie cluster eval --cases examples/sre-bot/evals/cases.json
```

[`evals/cases.json`](evals/cases.json) ships four portable cases: one capability
check that goes red if the Kubernetes connector disappears, and three behavioural
guarantees (uses the API for what metrics cannot answer, does not invent a count
on an empty result, refuses a write it has no tool for -- in its first sentence).

Every case carries a long `note` explaining what it anchors on and why, including
the anchors that were tried and rejected. That is the part worth copying. A case
whose expected value the model can reconstruct from the question, or recite from
`SKILL.md`, passes with no connector attached and gates nothing. Repo CI enforces
the floor of that: every committed case must go RED against a do-nothing fake
model, and must not be satisfiable by parroting its own input back.

When you add install-specific cases, hold each expected value to three
properties: absent from every file in the bundle, not standard Kubernetes or
Grafana vocabulary, and not reconstructible from the question.

**Check the bundle before deploying it:**

```bash
curie skill check --plugin-dir examples/sre-bot
```

---

## Troubleshooting

**The connector is in `CrashLoopBackOff` right after a deploy, and the logs show
a usage message.** A floating image tag drifted between the flag check and the
cluster's pull: the flags that existed when the args were written were gone by
deploy time, so the container printed its usage and exited. Pin by digest or an
immutable tag. Never `:latest`.

**`references non-existent secret key`, or the connector never starts.**
`curie secrets set` writes to Curie's own storage on your machine. It does NOT
put the value in the namespace -- only `curie cluster deploy` does, and a
git-flow push does not. Deploy from a CURRENT checkout: deploying from a stale
tree publishes a stale bundle over a live agent, which is a quieter and worse
failure than the one you were fixing.

**`curie cluster message` hangs for the whole timeout, and the worker log says
`UntrustedSlackEndpointError: refusing to send the Slack bot token to
http://...`.** `cluster message` serves its reply through a per-turn stub on
your machine, and the worker refuses to hand the Slack bot token to *any*
reply endpoint whose origin is not in `CURIE_SLACK_TRUSTED_ORIGINS` -- it does
not matter whether a token is even recorded. The guard fires on the endpoint
origin, not on token presence. Diagnose by reading the origin the worker log
names and comparing it against the configured list:

```bash
kubectl -n curie logs deployment/curie-worker --tail=60 | grep UntrustedSlackEndpointError
kubectl -n curie exec deployment/curie-worker -- printenv CURIE_SLACK_TRUSTED_ORIGINS
```

The usual cause on a multi-interface box: `cluster message` auto-detects a
local IP for its stub -- e.g. a Tailscale address -- that isn't in the
trusted-origins list. Pin the stub to an address that is:

```bash
curie cluster message --listen-host <ip-in-CURIE_SLACK_TRUSTED_ORIGINS> ...
```

`curie cluster comms --slack --disconnect` is not a fix for this error -- the
guard fires purely on the reply endpoint's origin, never on whether a token is
stale or even present, so disconnecting cannot clear the refusal. It remains
useful only for removing Slack comms from the install entirely:

```bash
curie cluster comms --slack --disconnect
```

That rolls the worker for you (`kubectl -n curie rollout restart
deployment/curie-worker`, then waits on the rollout), so the running pod picks
up the cleared tokens. If you cleared them some other way, run that restart by
hand or the pod keeps serving the stale Secret.

A timed-out turn's entry on the `curie:runs` valkey stream is reclaimed by the
platform's bounded-delivery/dead-letter backstop -- no manual XACK or other
stream surgery is needed.

**Deploy returns `422`, or a push is rejected with `approval_routes.unbound`.**
The bundle declares a route with no entry in this agent's `approval_routes`.
Bind every route the bundle declares, then redeploy.

**The bot says it escalated and no approval card appeared.** The named route is
not bound. Since #2436 a newly declared unbound route is refused at deploy; this
request-time escalation is the backstop for a binding removed after that check
ran. Bind it with
`curie cluster approvals <agent> --route-resolution <name>=<channel>` and
confirm with `--list-routes`. An unbound route never executes.

**The write fails AFTER a human approved it.** The worst shape: the approval is
spent, nothing rolled, and the operator's attention is gone. Both instances of
this were inside the connector's TLS setup -- a kubeconfig carrying its CA as
inline `certificate-authority-data` rather than a file path, and a fix that
wrote the CA to a temp file on a connector with a read-only root filesystem. Both
have regression tests in
[`connectors/k8s-write/test_server.py`](connectors/k8s-write/test_server.py). The
pattern is that the tool answers fine until a real cluster is on the other end,
so run it against one before trusting it.

**Nothing happens when you mention the bot in Slack.** Almost always a channel
bound by `#name` instead of by ID. The deploy reported success; the dispatcher
routes by ID and has nothing to route to.

**`forbidden: host not allowed` from the Grafana connector.** `mcp-grafana`
validates the `Host` header as a DNS-rebinding guard and defaults to loopback
only. Every name the sandbox may dial must appear in `-allowed-hosts`, which is
why `connectors.yaml` passes `${CURIE_ALLOWED_HOSTS}`. Do not hardcode the names
-- they embed the release, the agent and the namespace, so a hardcoded list is
wrong for at least one of your agents.

**The bot reports a namespace as healthy when it has no log coverage.** Empty is
not healthy. Most exporters emit a series only while a condition applies, so
"nothing crashlooping" and "the exporter is down" look identical, and a log query
against a namespace your shipper does not collect returns nothing rather than an
error. `SKILL.md` carries the rule; if your install has coverage gaps, name them
in the environment section so the bot can say which one it hit.

---

## What is in this bundle

| Path | What it is |
|---|---|
| `.claude-plugin/plugin.json` | Identity, starter prompts, and the exact write approval gate |
| `skills/sre-bot/SKILL.md` | The persona and answering rules -- **the main thing to edit** |
| `connectors.yaml` | Kubernetes read, gated write, Grafana and Tempo declarations |
| `deploy.yaml` | Named deploy targets: which agent, which environment, which channel |
| `evals/cases.json` | The falsifiable test suite / promotion gate |
| `manifests/read-access.yaml` | The read-only ServiceAccount, ClusterRole and token |
| `manifests/write-role.yaml` | The write identity, for the gated write path |
| `manifests/scale-role.yaml` | The scale identity, scoped to the `deployments/scale` subresource |
| `manifests/upgrade-role.yaml` | The upgrade identity -- **the widest grant here; read it first** |
| `connectors/k8s-write/` | Source, Dockerfile and tests for the one-tool write connector |
| `connectors/k8s-scale/` | Source, Dockerfile and tests for the one-tool scale connector |
| `connectors/self-upgrade/` | Source, Dockerfile and tests for the one-tool upgrade connector |
| `connectors/tempo/` | Source, Dockerfile and tests for the traces connector |
| `self-upgrade/` | The Job that redeploys this bot, and the CronJob template it runs from |

To change how the bot *behaves*, edit `SKILL.md`. To change what it can *reach*,
edit `connectors.yaml`.
