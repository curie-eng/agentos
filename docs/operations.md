# Operating the `cluster` target

This doc is the runbook for the **`cluster`** target: the Curie platform
running on a Kubernetes cluster (a Helm release). The same `curie` binary
installs and runs it, wrapping the umbrella Helm chart the way `linkerd` or
`cilium` wrap theirs. Every verb takes `--dry-run` to print the exact
`helm`/`kubectl` command line (secrets masked) without executing.

## The Kubernetes cluster

This doc covers the `cluster` target specifically. For `skill` or `local`
Targets, see the target comparison table in the
[README](../README.md#which-target-do-i-want) or [`cli/README.md`](../cli/README.md).

**Prerequisites:**

| Requirement | Why |
|---|---|
| `kubectl` and `helm` on PATH | Every `cluster` verb wraps one or both of them. |
| A reachable cluster | Every verb talks to the cluster's Kubernetes API server directly -- there's nothing to install onto or inspect without one. The chart's own preflights additionally need the `agents.x-k8s.io` Agent Sandbox CRDs (Custom Resource Definitions) installable and a NetworkPolicy-enforcing CNI (Container Network Interface) already present; see `charts/curie/README.md`. |
| `runsc` (gVisor) on every node -- **real models only** | Real-model installs refuse to start without it, as a safety measure against running a live model in a less-isolated sandbox. Skip the check with `--set security.gvisor.mode=off` if your cluster doesn't have it. Fake-model installs don't need it. |

**For testing**, pick between **k3s**, **kind**, and **minikube** based on
your host and how disposable the cluster needs to be. A single-node **k3s**
cluster (8 GB+ memory) is the lasting recommendation if you're on Linux --
its default kube-router CNI enforces NetworkPolicy out of the box, though
k3s itself only runs on Linux. **kind** and **minikube** work anywhere
Docker does and are fine for disposable local tests, but their Kubernetes
API server typically binds loopback, which can make it unreachable from a
pod; if `cluster message` can't auto-detect a pod-reachable host, pass
`--listen-host` explicitly (see `cli/README.md`).

**For production**, you'll likely point at a managed or self-hosted cluster
instead. Curie has no cluster-selection flag of its own -- every `cluster`
command just uses whatever `kubectl` and `helm` are already pointed at, so
switch clusters the normal `kubectl` way:

```bash
kubectl config use-context <your-production-context>
```

## Installing and inspecting the Curie platform on the cluster

### `curie cluster up`

Installs (or upgrades) Curie's Helm chart onto the cluster you're pointed at:

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
| `--github-token <token>` (or `CURIE_GITHUB_TOKEN`) | The Curie API's own GitHub credential, for cloning a PRIVATE repo during a git-flow bundle deploy and for posting the eval commit status. Goes to helm through a private mode-0600 values file, never a command-line argument, so it never appears in the helm command, the printed plan, or that plan's JSON. Prefer the environment variable: a token typed after the flag still sits in `curie`'s own argv, so it still reaches your shell history and `ps`. Omitting both on a later `cluster up` preserves whatever the release already has. Errors if combined with `--set api.githubToken=`. |
| `--clear-github-token` | Remove the stored GitHub credential. Not a revocation: the running API keeps the old token until its pod restarts (`cluster up` prints the restart command), and the token itself stays valid at GitHub until you revoke it there. |
| `--allow-egress-host <provider>` (repeatable) | Open runner egress on TCP 443 to a named model provider (`anthropic` or `openrouter`). |
| `--allow-web-egress <CIDR>` (repeatable) | Open runner egress on TCP 443 to an arbitrary CIDR (Classless Inter-Domain Routing block) -- for skill/tool web access, or a provider not covered above. |

A downloaded release binary needs no repo checkout; the chart resolves from
the version-pinned release asset by default.

**Egress is sealed by default.** A model credential alone opens no egress:
the sandbox stays fail-closed until you open its provider egress with one of
the two flags above. Neither flag bakes provider IPs into the binary --
only hostnames are resolved (to narrow `/32`+`/128` host routes) at install
time, because provider/CDN IPs rotate; re-run `up` to re-resolve if calls
start failing. `--allow-web-egress` is for agents whose skills need to
reach the open web -- a search tool, a weather lookup, anything beyond the
named model providers above: `curie cluster up --allow-web-egress
0.0.0.0/0` opens the open internet (still minus the `169.254.169.254`
metadata endpoint), or narrow the CIDR to a specific destination for a
tighter posture. A default-route value (`0.0.0.0/0`, `::/0`, any `/0`
prefix) prints a distinct rail-removal warning, since it removes the
default-deny rail for a prompt-injectable sandbox.

You don't need to worry about ordering when using the CLI flags together --
`cluster up` composes `--allow-egress-host` and `--allow-web-egress` into
one list automatically, with named-provider entries first and web-egress
CIDRs after.

### `curie cluster status`

```bash
curie cluster status
```

Reports whether the release is healthy, which pods are ready, and the URLs
to reach it -- including the web console, where you can see your agents,
their deployed versions, and their run history. That console URL includes a
`?api=1` parameter; leave it as-is when you open it, it's just what points
the console at this release's Curie API.

### `curie cluster down`

```bash
curie cluster down
```

| Flag | What it does |
|---|---|
| `--yes` | Skip the confirmation prompt. |

`curie cluster down` safely removes everything this release created, and
only what it created -- other things on the cluster are untouched,
including pre-existing namespaces and the Agent Sandbox CRDs.

It's also safe to re-run if something goes wrong. If the underlying
uninstall fails (say, a brief Kubernetes API-server hiccup), teardown doesn't just
stop -- it keeps going and cleans up whatever it safely can, so you're not
left with orphaned compute. If it still can't finish, the command tells
you exactly what to run next: an exact cleanup command you can copy-paste
once the cluster is reachable again. See ADR-0064 (Architecture Decision
Record; `docs/adr/0064-fail-forward-cluster-teardown.md`) for the full
fail-forward design.

## Deploying your plugin bundle onto the Curie platform

### Manually, with `curie cluster deploy`

This pushes your plugin bundle to the Curie API -- the control-plane component
that `cluster up` installs as part of the release.

```bash
curie cluster deploy --plugin-dir <bundle-dir>
```

| Flag / env var | What it does |
|---|---|
| `--plugin-dir <dir>` | The bundle directory to package and push. |
| `--api-url <url>` / `CURIE_API_URL` | Direct-dial this URL instead of self-plumbing a loopback tunnel. |
| `--api-key <key>` / `CURIE_API_KEY` | Override the auto-discovered API key. |

Beyond pointing it at your bundle, `cluster deploy` needs no other flags by
default: it automatically finds a way to reach the Curie API and
automatically finds the credentials to use, so
`curie cluster deploy --plugin-dir <bundle-dir>` just works.

Under the hood, it opens a secure local tunnel to the Curie API (so
nothing needs to be exposed publicly) and reads the API key straight out
of the release's own Kubernetes Secret -- the key is never printed or
stored anywhere in your shell history.

Override this only for a non-default setup: `--api-url` to talk to a
specific address instead of tunneling, or `--api-key` to use a specific key
instead of the auto-discovered one. As a safety check, if you point at a
plain `http://` URL, `cluster deploy` refuses to send an auto-discovered
key over it unencrypted -- pass `--api-key` explicitly to confirm that's
what you want, switch to `https://`, or drop `--api-url` to go back to the
safe default.

If something's not working: a discovery failure means the release's Secret
couldn't be read (pass `--api-key` yourself); a tunnel failure usually means
the release isn't healthy (check with `curie cluster status`); and if
nothing's been deployed yet, `curie cluster message` will say so plainly.

### Automatically, with git-flow

Beyond `curie cluster deploy`, a bundle can also deploy automatically on
every `git push`. Four things need to be true for a push to actually
promote:

1. **The agent's repo is set.** The webhook resolves which agent a push
   belongs to by matching the payload's `repo.full_name` (owner/name)
   against that agent's `repo_full_name`. This field is set when the agent
   is created (console or Curie API); `curie cluster deploy` only pushes a
   bundle to an agent that already exists and does not set this field. The
   match is case sensitive, so the stored `repo_full_name` must match
   GitHub's canonical owner and repository casing exactly, or the lookup
   finds no agent, the push is silently ignored, and (unlike a rejection)
   nothing is logged, so the only symptom is a green delivery in GitHub
   with nothing deployed.
2. **GitHub can reach the Curie API.** Add a webhook, in the repo's GitHub
   settings, to `<your-api-url>/github/webhook`. This requires the
   Curie API to be reachable from GitHub's servers (an ingress, a load
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
4. **The push comes from the configured clone origin.** The API derives the
   trusted clone URL from `GITHUB_CLONE_BASE` (chart value
   `api.githubCloneBase`), which defaults to `https://github.com`, and
   rejects any push whose `clone_url` doesn't match with the error code
   `git.origin_mismatch` -- the webhook still returns 200, so this fails
   silently from GitHub's side. The default covers github.com with no extra setup; set
   `GITHUB_CLONE_BASE` (or the chart's `api.githubCloneBase`) if your repos
   live elsewhere, such as GitHub Enterprise Server.

**Deploying a PRIVATE repo needs one more thing: a clone credential.**
Without it, git-flow can only deploy a public repository. A private one
fails with `git.archive_failed` (#1058). Supply the API's GitHub credential
with `--github-token` on `curie cluster up` (or, to keep it out of your
shell history and `ps`, the `CURIE_GITHUB_TOKEN` environment variable
instead; see the flag reference above for how it's kept out of the helm
command line too). A later plain `cluster up` that passes neither preserves
whatever the release already has, so you only set it once. Changing or
clearing it (`--clear-github-token`) does not restart the API pod
automatically; `cluster up` prints the exact restart command to run, and
until you run it the API keeps using the old value.

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
| `--continue` | Reuse the same conversation thread as your last `cluster message` call. |
| `--thread <id>` | Continue a specific earlier conversation thread by ID, instead of the most recent one. |
| `--force-wire` | `cluster message` normally refuses to run against a release that's already wired to a real Slack workspace, since driving it would send replies into that workspace instead. Pass `--force-wire` to override that guard. |

This exercises a deployed release end to end with no Slack at all. It:

- simulates the exact Slack event your bot would receive
- runs it through the real deployed worker and a real Kubernetes sandbox
- prints the reply

`cluster message` handles the port-forwards, channel resolution, and stub
routing itself, so none of that is something you need to set up. `--continue`
reads its saved context from `.curie/last-turn.json` in the current <!-- doclint:ignore-line -->
directory.

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

`curie cluster comms --slack` wires your release up to a real Slack
workspace: it stores the tokens you pass, points the release at Slack
instead of the local `cluster message` stub, and restarts the affected pods
so the change takes effect immediately.

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
