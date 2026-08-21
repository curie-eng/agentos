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

**Out of the box it cannot change anything.** Every shipped tool is
`readOnlyHint`. Kubernetes access uses a ServiceAccount that cannot read
Secrets or delete anything, and Grafana access uses the Viewer role. There is no
write tool, so there is nothing to gate, so no approval policy is declared at
all.

**One write verb ships in the box, switched off.** Enabling it takes a few
deliberate steps and gives the bot exactly one thing it can change: rolling a
single named Deployment, behind a human approval card. See
[Level up: the gated write path](#level-up-the-gated-write-path).

This is the repo's most complete example bundle. It is a real bot, generalised:
a skill, four connectors (three on, one off), the RBAC to stand it up, a
purpose-built write connector with its source and tests, deploy targets, and a
falsifiable eval suite.

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

This installs the bot and its self referential observability stack. It requires
no values files, connector edits, or supplied credentials.

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
asked can never be the person who approves** -- the platform blocks
self-approval.

Read the four-layer table above before starting, and
[`manifests/write-role.yaml`](manifests/write-role.yaml) before applying it.

**1. Create the write identity.**

Edit the namespace and the `resourceNames` list in
[`manifests/write-role.yaml`](manifests/write-role.yaml) first -- one Deployment,
not a list, unless someone has actually asked for the second one. Then:

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

**The credential comes before the connector, and that order is load-bearing.** A
declared connector secret with no stored value fails the deploy, so a bundle
that enables `k8s-write` before `K8S_WRITE_KUBECONFIG` exists cannot deploy at
all. That is why this step sits ahead of the uncomment rather than beside it.

**3. Turn the connector on.**

Uncomment the `k8s-write:` block in [`connectors.yaml`](connectors.yaml) and set
`K8S_WRITE_ALLOWLIST` to `<namespace>/<deployment>` -- the same pair you put in
`resourceNames`. Stating the ceiling in two places is deliberate: a ceiling
stated once is a ceiling that moves when someone edits the other place.

There is no `image:` line to fill in. The block declares a `build:` -- the source
directory and the platforms -- and step 4 resolves it to a digest.

**4. Build the connector image.**

```bash
curie build --plugin-dir examples/sre-bot --registry <your-registry>
```

One command, after the uncomment rather than before it: `curie build` builds
what the file currently declares, so a still-commented connector is not built.
It builds every declared `build:` connector on every platform its `platforms:`
line names, pushes them, and writes the resolved digests to
`connectors.lock.yaml` beside `connectors.yaml`. The deploy renders those
digests and nothing else, so the "pin it, never `:latest`" rule is enforced by
the artifact rather than by remembering.

There is no public image on purpose: the allowlist and the credential are yours,
and so is the artifact. Multi-arch is not a flag you pass either -- the
`platforms:` line in [`connectors.yaml`](connectors.yaml) carries it, because a
single-arch image passes CI and then fails to pull on a node of the other
architecture with "no matching manifest", which reads as a registry problem.

If your registry defaults new packages to private, flip this one to public or
give the cluster a pull secret. An anonymous pull otherwise 403s and surfaces as
`ImagePullBackOff`.

`connectors.lock.yaml` is gitignored in this example. It records your registry
and the digests your build resolved, so it belongs to an install; the deploy
packs it from your working tree.

**5. Declare the approval gate.**

Add this to `.claude-plugin/plugin.json`. It is not shipped, because bundle
validation rejects a gate naming an undeclared connector -- so a gate for a
commented-out connector would fail the build for everyone who never enables it.
The consequence is that the gate travels with step 3's uncomment, in the same
change: enabling the connector without it deploys an ungated write verb.

```json
  "approvalPolicy": {
    "gates": [
      {
        "gate": "mcp__k8s-write__restart_deployment",
        "route": "sre-approvals"
      }
    ]
  }
```

**THE TOOL NAME IS NOT WHAT THE ERROR MESSAGE TELLS YOU.** It is
`mcp__k8s-write__restart_deployment`, NOT
`mcp__plugin_sre-bot_k8s-write__restart_deployment`. Curie connectors are
platform-supplied servers, named like `mcp__curie__request_approval` with no
plugin infix; only bundle-loaded MCP servers take the `plugin_<bundle>_` form.
The deploy error advises the prefixed form. Following it yields a gate that
validates, deploys, and never fires.

**6. Redeploy.**

```bash
curie cluster deploy --plugin-dir examples/sre-bot
```

**7. Bind the route, or the card has nowhere to post.**

```bash
curie cluster approvals sre-bot --route sre-approvals=C0EXAMPLE1
curie cluster approvals sre-bot --list-routes
```

An unbound route fails SAFE -- it escalates to a human rather than executing --
but you will see `approval route 'sre-approvals' is not bound for agent ...;
escalating rather than routing the card` and no card will appear.

**`--route` and `--gate` are FULL REPLACEMENTS, not additive.** A write replaces
the whole map, so name every route you want on every invocation. Passing one
route to add it silently drops the others.

**8. Approve one.**

In Slack, someone other than the requester clicks the card. Self-approval is
blocked by the platform, which in a one-person workspace is a dead end for a
Slack-initiated request. The way through is to drive the turn from the CLI
instead, which makes the CLI's own synthetic user the requester rather than you.

**Use two shells, and give the asking one a long timeout.** This is the pattern
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
curie cluster approvals sre-bot --resolve <approval-id> --as <someone-else> --actor-channel C0EXAMPLE1
```

`--as` must not name the requester. Raise `--timeout-secs` further if a human is
doing the deciding rather than you in the next terminal.

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

Then remove it properly: comment the `k8s-write` block in
[`connectors.yaml`](connectors.yaml) and redeploy. The connector disappears and
so does the tool.

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
http://...`.** A previous `curie cluster comms --slack` recorded a bot token in
the release, and `cluster up` preserves it across upgrades. `cluster message`
serves its reply through a per-turn stub on your machine, and the worker will
not hand a Slack bot token to an endpoint that is not Slack, so it refuses to
deliver and the CLI waits forever. This is a platform bug rather than anything in
this bundle, and it bites anyone who tries Slack first and the CLI second. Clear
the recorded tokens:

```bash
curie cluster comms --slack --disconnect
```

That rolls the worker for you (`kubectl -n curie rollout restart
deployment/curie-worker`, then waits on the rollout), so the running pod picks
up the cleared tokens. If you cleared them some other way, run that restart by
hand or the pod keeps serving the stale Secret.

**The bot says it escalated and no approval card appeared.** The route named in
`approvalPolicy` is not bound for that agent. Bind it with
`curie cluster approvals <agent> --route <name>=<channel>` and confirm with
`--list-routes`. This fails safe -- an unbound route escalates, it never executes.

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
| `.claude-plugin/plugin.json` | Identity, starter prompts. No `approvalPolicy`; see the optional write section |
| `skills/sre-bot/SKILL.md` | The persona and answering rules -- **the main thing to edit** |
| `connectors.yaml` | What the bot needs running; Curie derives the Kubernetes |
| `deploy.yaml` | Named deploy targets: which agent, which environment, which channel |
| `evals/cases.json` | The falsifiable test suite / promotion gate |
| `manifests/read-access.yaml` | The read-only ServiceAccount, ClusterRole and token |
| `manifests/write-role.yaml` | The write identity, for the optional write path |
| `connectors/k8s-write/` | Source, Dockerfile and tests for the one-tool write connector |
| `connectors/tempo/` | Source, Dockerfile and tests for the traces connector |

To change how the bot *behaves*, edit `SKILL.md`. To change what it can *reach*,
edit `connectors.yaml`.
