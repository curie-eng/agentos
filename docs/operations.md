# Operating the `cluster` target

This doc is the runbook for the **`cluster`** target: the Curie platform
running on a Kubernetes cluster (a Helm release). The same `curie` binary
installs and runs it, wrapping the umbrella Helm chart the way `linkerd` or
`cilium` wrap theirs. Every verb takes `--dry-run` to print the exact
`helm`/`kubectl` command line (secrets masked) without executing.

`cluster` is the heaviest of three CLI targets. Reach for a lighter one when it
answers your question:

## Which target do I want?

| Target | What runs | Slack | Kubernetes | Verbs | Reach for it to |
|---|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | stub by default, optional real Slack with `--slack` | none | `up` `down` `status` `comms` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `comms` `message` `deploy` | Operate and drive a deployed cluster release (this doc). |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, while `local` and `cluster` add `comms` plus `deploy`. The `skill`
target is the runner-only loop; `local` and `cluster` add the full platform in
front of the identical runner and ACI (Agent Container Interface). For the `skill` and `local` targets see
[`cli/README.md`](../cli/README.md) and the
[README](../README.md#which-target-do-i-want); the rest of this doc is `cluster`.

Prerequisites: `kubectl` and `helm` on PATH, pointed at a reachable cluster
(the `agents.x-k8s.io` Agent Sandbox CRDs (Custom Resource Definitions) and a
NetworkPolicy-enforcing CNI (Container Network Interface) are
installed by the chart's preflights; see `charts/curie/README.md`).

## Choosing a cluster

A single-node **k3s** cluster on a Linux host (8 GB+ memory) is the lasting
recommendation -- its default kube-router CNI enforces NetworkPolicy out of
the box. **kind** and **minikube** work fine for disposable local tests, but
their API server typically binds loopback, which can make it unreachable
from a pod. If `cluster message` can't auto-detect a pod-reachable host,
pass `--listen-host` explicitly (see `cli/README.md`).

Real-model installs additionally require `runsc` (gVisor) on every node
unless kernel isolation is explicitly disabled (`--set
security.gvisor.mode=off`); fake-model installs work without it.

## Install and inspect

- **`curie cluster up`** runs `helm upgrade --install` using the chart resolved
  from the version-pinned release asset by default, so a downloaded release
  binary needs no repo checkout.
  - Pass `--chart <path-or-tgz>` to override with a local chart for chart
    development. For local development, override resolved artifacts with
    `-f <compose>`, `--chart <path-or-tgz>`, and `--image <ref>`.
  - It installs into the `curie` namespace, exposing the UI and Langfuse on
    node ports (pass `--no-expose` to keep them ClusterIP-only).
  - **Credentials and the fake model.** It reads `CURIE_CREDENTIALS`
    (deprecated alias `CURIE_MODEL_CREDENTIALS`). When the env var is set, it
    switches the runner off the fake model and forwards the credential
    through the masked `--set` machinery (so `--dry-run` never prints it).
    When it is absent, the release installs sealed (canned replies) and `up`
    warns that replies stay canned until the env var is set and `up` is
    re-run. Pass `--fake-model` to force the sealed install even when the
    credential is present (a dev/CI escape hatch).
  - **Egress is sealed by default.** A model credential alone opens no
    egress: the sandbox stays fail-closed, so the model host is unreachable
    until you open its provider egress explicitly. When a credential is
    present but no egress was opened, `up` warns the sandbox is sealed and
    the model is unreachable, naming both flags below.
  - **Named-provider egress.** Pass `--allow-egress-host <provider>`
    (repeatable) to open runner egress on TCP 443 to a named model provider
    -- one of `anthropic` or `openrouter`. Each maps to that provider's API
    hostname (`anthropic` -> `api.anthropic.com`, `openrouter` ->
    `openrouter.ai`), which the CLI resolves to narrow host routes (`/32` +
    `/128`) at install time, from the machine running `cluster up` (so the
    resolved IPs can differ from the runner's in-cluster view under GeoDNS
    or split-horizon DNS). The set is intentionally limited to the two
    providers the runner can drive today (`anthropic` via `sk-ant-`,
    `openrouter` via `sk-or-`); others (OpenAI, Gemini, the
    base-URL-override providers) are layered in only once the runner
    supports them. No provider IPs are baked into the binary, only
    hostnames, because provider/CDN IPs rotate; if calls start failing
    after a rotation, re-run `up` to re-resolve. An unknown
    `--allow-egress-host` value is a usage error listing the accepted
    providers and pointing at `--allow-web-egress` for arbitrary
    destinations.
  - **Arbitrary web egress.** Pass `--allow-web-egress <CIDR>` (Classless
    Inter-Domain Routing block, repeatable) to open runner egress on TCP 443
    to each declared CIDR for skill/tool web access or for a destination no
    named provider covers; omit both flags and egress stays sealed. This is
    the platform enablement the weather example (#36) needs, whose skill
    answers via a live web search: `curie cluster up --allow-web-egress
    0.0.0.0/0` opens the open internet (still minus the `169.254.169.254`
    metadata endpoint the chart carves out of `0.0.0.0/0`), or narrow the
    CIDR to a specific web-search provider for a tighter posture. When a
    declared value is a default route (`0.0.0.0/0`, `::/0`, or any `/0`
    prefix), `up` prints a distinct rail-removal warning -- opening egress
    to the whole internet removes the default-deny rail for a
    prompt-injectable sandbox, so prefer a narrow CIDR unless you genuinely
    need the open internet.
  - **Egress ordering.** The declared egress entries occupy the
    `allowedEgress` array in order: provider host routes from
    `--allow-egress-host` take the leading indices only when that flag is
    passed, followed by any `--allow-web-egress` CIDRs. The raw helm
    equivalent of a single web-egress rule (with no provider egress) is
    `--set 'security.networkPolicy.allowedEgress[0].cidr=0.0.0.0/0'` plus
    `...[0].ports[0].protocol=TCP` and `...[0].ports[0].port=443`; shift the
    index up by one for each preceding `--allow-egress-host` entry so the
    array has no gap.
- **`curie cluster status`** reports release health, pod readiness, and the
  access URLs; the UI URL carries `?api=1`, so it opens wired to the
  in-cluster API (the deployed UI proxies `/api/` there).
- **`curie cluster down`** uninstalls the release and sweeps its namespaces.
  - It deletes only the namespaces this release created, identified by the
    ownership label `up` stamped on them (`curietech.ai/created-by=<release>`);
    pre-existing (unlabeled) namespaces and the `agents.x-k8s.io` CRDs are
    left untouched. It prompts before deleting unless `--yes` is passed.
  - **Fail-forward teardown.** If `helm uninstall` fails (for example a
    transient API-server blip), teardown does not abort: the
    ownership-scoped namespace sweep still runs so compute is not left
    orphaned. If the sweep's label selector matches nothing (for example a
    pre-existing namespace, which is never stamped with the ownership
    label), the error message says so plainly rather than claiming
    namespaces were removed.
  - **If teardown still cannot complete,** the command exits nonzero (exit 3
    for a transient/retryable failure, exit 1 otherwise) and prints an
    exact resumable cleanup command, also carried in the `--json` `{error,
    fix}` payload, to run once the API server is reachable. When both the
    uninstall and the sweep are still outstanding, that resumable command
    aggregates both steps' exit statuses so re-running it verbatim cannot
    misreport success while the release record is still stale. See
    ADR-0064 (Architecture Decision Record;
    `docs/adr/0064-fail-forward-cluster-teardown.md`).

## Connecting Slack

Use `curie cluster comms --slack` to wire a real Slack workspace onto the
release. It is a thin `helm upgrade --reuse-values` wrapper that sets the
dispatcher's app and bot tokens and, on connect, clears
`worker.slackApiBaseUrl=` to un-wire any `curie cluster message` stub routing.
After the upgrade, it also restarts and waits for the worker (and, on connect,
the dispatcher) so the running pods pick up the changed tokens. A Secret
change alone does not roll pods whose token comes from a `secretKeyRef` env
var.

Connect:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
curie cluster comms --slack
```

Disconnect:

```bash
curie cluster comms --slack --disconnect
```

Dry run:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
curie cluster comms --slack --dry-run
```

The env-backed token values are masked in dry-run output and are never printed
in full.

### Local compose comms

Use `curie local comms --slack` to wire the compose stack to a real Slack
workspace. It reads `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`, masks both values
in printed commands, starts the dispatcher, and points the worker at real
Slack.

Disconnect:

```bash
curie local comms --slack --disconnect
```

Disconnect stops the dispatcher and restores the local Slack stub so
`curie local message` keeps working.

Dry run:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
curie local comms --slack --dry-run
```

Dry run prints the compose command with masked token values and does not change
the stack.

## Deploy a bundle to the cluster

Before `curie cluster message` can drive an agent, a bundle must be deployed to
the in-cluster platform API with `curie cluster deploy`. Per ADR-0057, with no
`--api-url`, `cluster deploy` self-plumbs a `kubectl port-forward` to
`svc/<release>-api` (a loopback tunnel). It dials `http://localhost:<port>`
directly -- no manual port-forward and no UI NodePort proxy involved:

```bash
curie cluster deploy --plugin-dir <bundle-dir>
```

With no `--api-key`/`CURIE_API_KEY` either, the key is auto-discovered by
reading `api.apiKey` out of the release's `<release>-secrets` Secret (decoded
server-side, so the plaintext never lands in argv). The discovered key travels
only in the `X-API-Key` header over the loopback tunnel, never over the
cleartext UI `/api` NodePort proxy that ADR-0024 used for this path. Pass
`--api-key` explicitly (or set `CURIE_API_KEY`) to override discovery with
your own key.

An explicit `--api-url` (e.g. `http://<node>:30080/api`, ADR-0024's UI proxy
still available as the escape hatch) or `CURIE_API_URL` direct-dials the given
URL exactly as given, with no tunnel. If the auto-discovered key would then
travel over plain `http://`, `cluster deploy` refuses rather than leak it on
the wire. To proceed, either pass `--api-key` explicitly to acknowledge, use
an `https://` URL, or omit `--api-url` to go back over the loopback tunnel.

Key discovery fails with a usage error telling you to pass `--api-key` when the
release's `<release>-secrets` Secret cannot be read. The port-forward itself
fails with a hint to check `curie cluster status` if the release is not
healthy.

Without a deploy, `curie cluster message` fails with `no agents are deployed on
the platform API`.

## Connecting a repo for git-flow deploys

Beyond `curie cluster deploy`, a bundle can also deploy automatically on every
`git push`. Three things need to be true for a push to actually promote:

1. **The agent's repo is set.** The webhook resolves which agent a push
   belongs to by matching the payload's `repo.full_name` (owner/name) against
   that agent's `repo_full_name`. This field is set when the agent is
   created (console or API); `curie cluster deploy` only pushes a bundle to
   an agent that already exists and does not set this field.
2. **GitHub can reach the API.** Add a webhook, in the repo's GitHub settings,
   to `<your-api-url>/github/webhook`. This requires the cluster's API to be
   reachable from GitHub's servers (an ingress, a load balancer, or a tunnel);
   how you expose it is an infrastructure decision this chart does not make
   for you.
3. **The webhook secret matches.** GitHub signs each delivery
   (`x-hub-signature-256`), verified against the chart-managed
   `githubWebhookSecret`. Retrieve the generated value from the same Secret
   `cluster deploy` reads its API key from:
   ```bash
   kubectl get secret <release>-secrets -o jsonpath='{.data.githubWebhookSecret}' | base64 -d
   ```
   and paste it into the webhook's secret field.

Once wired, a push to the agent's dev branch builds and deploys under its dev
bot identity; a push or merge to its prod branch promotes that same built
artifact without rebuilding.

## Driving a deployed cluster with zero Slack

`curie cluster message "..."` exercises a deployed release end to end with no
Slack at all. It:

- stands up a local Slack API stub
- self-manages the kubectl port-forwards
- resolves the target agent's channel from the API
- points the deployed worker at the stub (`helm upgrade --reuse-values`)
- enqueues the exact event a Slack mention would produce
- boots the real Kubernetes sandbox
- prints the reply

This lets a developer iterate on an agent built for someone else's workspace
with no Slack access. It refuses to hijack a release that is already
connected to a real workspace unless `--force-wire`. Full flag reference and
the multi-turn `--thread` flow are in [`cli/README.md`](../cli/README.md).

## Bridging to the local dev stack

`curie local up|down|status` wraps the local compose stack. A no-checkout
release binary uses the pinned `compose.release.yaml` release asset, while
repo development still uses `compose.dev.yaml` -- the inner loop and the
cluster share one CLI either way.

`local up` brings up the full product stack (API + worker alongside the
backing stores, plus the console UI at `http://localhost:28080/?api=1`). From
there, `curie local deploy --api-url http://localhost:28000` followed by
`curie local message "..."` drives a real queue -> worker -> sandboxed runner
-> reply roundtrip with no Slack and no Kubernetes. See the middle-mode
runbook in the [README](../README.md#quickstart).

## First-install findings

Notes from the first installs of the chart on fresh clusters, kept for the next
operator.

- **The agent-sandbox controller is opt-in.** The chart ships the agent-sandbox
  CRDs, but the vendored controller is gated behind
  `agentSandbox.controller.deploy`. A cluster that has the CRDs but no
  controller silently never binds claims, so a first install must set
  `agentSandbox.controller.deploy=true` unless the cluster already runs the
  controller.
- **gVisor stays off without runsc on the node.** Use the `values-e2e-nogvisor`
  overlay on nodes without `runsc`. All other security rails were verified ON in
  the first fresh-cluster install: default-deny egress, metadata-endpoint block,
  read-only rootfs, non-root, and per-agent secret isolation.
- **langfuse-web restarts ~2x during first boot** while ClickHouse and Postgres
  come up, then stabilizes. This is startup ordering, not a crashloop; do not
  treat the early restarts as a failure.
- **Exactly one Slack Socket Mode owner at a time.** Stop a local dispatcher
  before enabling `dispatcher.deploy=true` in the chart, and stop the in-cluster
  dispatcher before switching back to a local one for dev.
- **kube-router applies NetworkPolicy a few seconds after pod start.** A
  brand-new pod can see open egress for the first seconds before the policy
  lands. This is functionally irrelevant for runners (the first model call comes
  later) but worth knowing when reading probe output from the first seconds of a
  pod's life.
