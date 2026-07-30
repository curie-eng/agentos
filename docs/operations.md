# Operating the `cluster` target

This doc is the runbook for the **`cluster`** target: the Curie platform
running on a Kubernetes cluster (a Helm release). The same `curie` binary
installs and runs it, wrapping the umbrella Helm chart the way `linkerd` or
`cilium` wrap theirs. Every verb takes `--dry-run` to print the exact
`helm`/`kubectl` command line (secrets masked) without executing.

## The Kubernetes cluster

This doc covers the `cluster` target specifically. If `skill` or `local`
answers your question instead, see the target comparison table in the
[README](../README.md#which-target-do-i-want) or [`cli/README.md`](../cli/README.md).

**Prerequisites:**

| Requirement | Why |
|---|---|
| `kubectl` and `helm` on PATH | Every `cluster` verb wraps one or both of them. |
| A reachable cluster | The chart's preflights install the `agents.x-k8s.io` Agent Sandbox CRDs (Custom Resource Definitions) and expect a NetworkPolicy-enforcing CNI (Container Network Interface) already on the cluster; see `charts/curie/README.md`. |
| `runsc` (gVisor) on every node -- **real models only** | A real-model install fails closed without it, unless kernel isolation is explicitly disabled (`--set security.gvisor.mode=off`). Fake-model installs work without it. |

**Which cluster to point at:** a single-node **k3s** cluster on a Linux host
(8 GB+ memory) is the lasting recommendation -- its default kube-router CNI
enforces NetworkPolicy out of the box. **kind** and **minikube** work fine
for disposable local tests, but their API server typically binds loopback,
which can make it unreachable from a pod; if `cluster message` can't
auto-detect a pod-reachable host, pass `--listen-host` explicitly (see
`cli/README.md`).

## Installing and inspecting the Curie platform on the cluster

### `curie cluster up`

Installs (or upgrades) Curie onto the cluster you're pointed at:

```bash
curie cluster up
```

| Flag / env var | What it does |
|---|---|
| `--chart <path-or-tgz>` | Install from a local chart instead of the pinned release asset (for chart development). |
| `-f <compose>` | Override a resolved local-dev artifact path. |
| `--image <ref>` | Override a resolved image reference. |
| `--no-expose` | Keep the UI and Langfuse ClusterIP-only instead of exposing them on node ports. |
| `CURIE_CREDENTIALS` (alias `CURIE_MODEL_CREDENTIALS`) | A real model credential. Present -> installs live (forwarded through masked `--set` machinery, so `--dry-run` never prints it). Absent -> installs sealed (canned replies). |
| `--fake-model` | Force a sealed install even when a credential is present (a dev/CI escape hatch). |
| `--allow-egress-host <provider>` (repeatable) | Open runner egress on TCP 443 to a named model provider (`anthropic` or `openrouter`). |
| `--allow-web-egress <CIDR>` (repeatable) | Open runner egress on TCP 443 to an arbitrary CIDR (Classless Inter-Domain Routing block) -- for skill/tool web access, or a provider not covered above. |

A downloaded release binary needs no repo checkout; the chart resolves from
the version-pinned release asset by default.

**Egress is sealed by default.** A model credential alone opens no egress:
the sandbox stays fail-closed until you open its provider egress with one of
the two flags above. Neither flag bakes provider IPs into the binary --
only hostnames are resolved (to narrow `/32`+`/128` host routes) at install
time, because provider/CDN IPs rotate; re-run `up` to re-resolve if calls
start failing. The weather example (#36) is what `--allow-web-egress` is
for: `curie cluster up --allow-web-egress 0.0.0.0/0` opens the open internet
(still minus the `169.254.169.254` metadata endpoint), or narrow the CIDR to
a specific provider for a tighter posture. A default-route value
(`0.0.0.0/0`, `::/0`, any `/0` prefix) prints a distinct rail-removal
warning, since it removes the default-deny rail for a prompt-injectable
sandbox.

The two egress flags compose into one `allowedEgress` array, in order:
`--allow-egress-host` entries take the leading indices, followed by any
`--allow-web-egress` CIDRs. The raw helm equivalent of a single web-egress
rule (no provider egress) is `--set
'security.networkPolicy.allowedEgress[0].cidr=0.0.0.0/0'` plus
`...[0].ports[0].protocol=TCP` and `...[0].ports[0].port=443`; shift the
index up by one for each preceding `--allow-egress-host` entry.

### `curie cluster status`

```bash
curie cluster status
```

Reports release health, pod readiness, and the access URLs. The UI URL
carries `?api=1`, so it opens wired to the in-cluster API (the deployed UI
proxies `/api/` there).

### `curie cluster down`

```bash
curie cluster down
```

| Flag | What it does |
|---|---|
| `--yes` | Skip the confirmation prompt. |

Uninstalls the release and sweeps its namespaces: only the namespaces this
release created (identified by the ownership label
`curietech.ai/created-by=<release>`) are deleted. Pre-existing (unlabeled)
namespaces and the `agents.x-k8s.io` CRDs are left untouched.

**Fail-forward teardown.** If `helm uninstall` fails (for example a
transient API-server blip), teardown does not abort -- the ownership-scoped
namespace sweep still runs so compute is not left orphaned. If the sweep's
label selector matches nothing (for example a pre-existing namespace, which
is never stamped with the ownership label), the error message says so
plainly rather than claiming namespaces were removed.

If teardown still cannot complete, the command exits nonzero (exit 3 for a
transient/retryable failure, exit 1 otherwise) and prints an exact resumable
cleanup command (also carried in the `--json` `{error, fix}` payload) to run
once the API server is reachable. That resumable command aggregates both
the uninstall and the sweep's exit statuses, so re-running it verbatim
cannot misreport success while the release record is still stale. See
ADR-0064 (Architecture Decision Record;
`docs/adr/0064-fail-forward-cluster-teardown.md`).

## Deploying your plugin bundle onto the Curie platform

### Manually, with `curie cluster deploy`

```bash
curie cluster deploy --plugin-dir <bundle-dir>
```

| Flag / env var | What it does |
|---|---|
| `--plugin-dir <dir>` | The bundle directory to package and push. |
| `--api-url <url>` / `CURIE_API_URL` | Direct-dial this URL instead of self-plumbing a loopback tunnel. |
| `--api-key <key>` / `CURIE_API_KEY` | Override the auto-discovered API key. |

With no `--api-url`, `cluster deploy` self-plumbs a `kubectl port-forward`
to `svc/<release>-api` (a loopback tunnel, per ADR-0057) and dials
`http://localhost:<port>` directly -- no manual port-forward and no UI
NodePort proxy involved. With no `--api-key` either, the key is
auto-discovered by reading `api.apiKey` out of the release's
`<release>-secrets` Secret (decoded server-side, so the plaintext never
lands in argv); the discovered key travels only in the `X-API-Key` header
over the loopback tunnel, never over the cleartext UI `/api` NodePort proxy
that ADR-0024 used for this path.

An explicit `--api-url` (e.g. `http://<node>:30080/api`, ADR-0024's UI proxy
still available as the escape hatch) or `CURIE_API_URL` direct-dials the
given URL exactly as given, with no tunnel. If the auto-discovered key would
then travel over plain `http://`, `cluster deploy` refuses rather than leak
it on the wire. To proceed, either pass `--api-key` explicitly to
acknowledge, use an `https://` URL, or omit `--api-url` to go back over the
loopback tunnel.

Key discovery fails with a usage error telling you to pass `--api-key` when
the release's `<release>-secrets` Secret cannot be read. The port-forward
itself fails with a hint to check `curie cluster status` if the release is
not healthy. Without a deploy, `curie cluster message` fails with `no
agents are deployed on the platform API`.

### Automatically, with git-flow

Beyond `curie cluster deploy`, a bundle can also deploy automatically on
every `git push`. Three things need to be true for a push to actually
promote:

1. **The agent's repo is set.** The webhook resolves which agent a push
   belongs to by matching the payload's `repo.full_name` (owner/name)
   against that agent's `repo_full_name`. This field is set when the agent
   is created (console or API); `curie cluster deploy` only pushes a bundle
   to an agent that already exists and does not set this field.
2. **GitHub can reach the API.** Add a webhook, in the repo's GitHub
   settings, to `<your-api-url>/github/webhook`. This requires the
   cluster's API to be reachable from GitHub's servers (an ingress, a load
   balancer, or a tunnel); how you expose it is an infrastructure decision
   this chart does not make for you.
3. **The webhook secret matches.** GitHub signs each delivery
   (`x-hub-signature-256`), verified against the chart-managed
   `githubWebhookSecret`. Retrieve the generated value from the same Secret
   `cluster deploy` reads its API key from:
   ```bash
   kubectl get secret <release>-secrets -o jsonpath='{.data.githubWebhookSecret}' | base64 -d
   ```
   and paste it into the webhook's secret field.

Once wired, a push to the agent's dev branch builds and deploys under its
dev bot identity; a push or merge to its prod branch promotes that same
built artifact without rebuilding.

## Talking to your agent

The plugin bundle you just deployed is the agent's backend. There are two
frontends that can talk to it: your terminal (no Slack involved) or a real
Slack workspace.

### Without Slack, from the terminal

```bash
curie cluster message "hello, are you there?"
```

| Flag | What it does |
|---|---|
| `--thread` | Continue a multi-turn conversation (see `cli/README.md` for the full flow). |
| `--force-wire` | Allow driving a release that's already connected to a real Slack workspace (refused by default). |

This exercises a deployed release end to end with no Slack at all. It:

- stands up a local Slack API stub
- self-manages the kubectl port-forwards
- resolves the target agent's channel from the API
- points the deployed worker at the stub (`helm upgrade --reuse-values`)
- enqueues the exact event a Slack mention would produce
- boots the real Kubernetes sandbox
- prints the reply

This lets a developer iterate on an agent built for someone else's
workspace with no Slack access. Full flag reference is in
[`cli/README.md`](../cli/README.md).

### Connecting Slack

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
curie cluster comms --slack
```

| Flag | What it does |
|---|---|
| `--disconnect` | Disconnect Slack and revert to CLI-driven testing. |
| `--dry-run` | Print the masked `helm` command without executing (env-backed token values are masked, never printed in full). |

`curie cluster comms --slack` is a thin `helm upgrade --reuse-values`
wrapper that sets the dispatcher's app and bot tokens and, on connect,
clears `worker.slackApiBaseUrl=` to un-wire any `curie cluster message` stub
routing. After the upgrade, it also restarts and waits for the worker (and,
on connect, the dispatcher) so the running pods pick up the changed tokens
-- a Secret change alone does not roll pods whose token comes from a
`secretKeyRef` env var.

For the `local`-target equivalent (`curie local comms --slack`), see
[`cli/README.md`](../cli/README.md).

## Known gotchas

Notes from the first installs of the chart on fresh clusters, kept for the
next operator.

- **The agent-sandbox controller is opt-in.** The chart ships the
  agent-sandbox CRDs, but the vendored controller is gated behind
  `agentSandbox.controller.deploy`. A cluster that has the CRDs but no
  controller silently never binds claims, so a first install must set
  `agentSandbox.controller.deploy=true` unless the cluster already runs the
  controller.
- **gVisor stays off without runsc on the node.** Use the
  `values-e2e-nogvisor` overlay on nodes without `runsc`. All other
  security rails were verified ON in the first fresh-cluster install:
  default-deny egress, metadata-endpoint block, read-only rootfs, non-root,
  and per-agent secret isolation.
- **langfuse-web restarts ~2x during first boot** while ClickHouse and
  Postgres come up, then stabilizes. This is startup ordering, not a
  crashloop; do not treat the early restarts as a failure.
- **Exactly one Slack Socket Mode owner at a time.** Stop a local dispatcher
  before enabling `dispatcher.deploy=true` in the chart, and stop the
  in-cluster dispatcher before switching back to a local one for dev.
- **kube-router applies NetworkPolicy a few seconds after pod start.** A
  brand-new pod can see open egress for the first seconds before the policy
  lands. This is functionally irrelevant for runners (the first model call
  comes later) but worth knowing when reading probe output from the first
  seconds of a pod's life.
