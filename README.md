# Curie

[![CI](https://github.com/curie-eng/curie/actions/workflows/ci.yaml/badge.svg)](https://github.com/curie-eng/curie/actions/workflows/ci.yaml)

Open-source, self-hostable developer platform for Slack-based agents. Connect
Slack, author a Claude-Code-format plugin (skills + tools + MCP), deploy it as
a versioned bot identity, and get traces, evals, budgets, and git-flow for
free.

New here? Start with [`QUICKSTART.md`](QUICKSTART.md) to get your first agent
reply in about a minute. Then read [`docs/vision.md`](docs/vision.md) for what
Curie is, who it is for, and what it could become. It is the north star we
hold new features against.

"Relay" is this project's internal codename (repo, commits, internal docs);
"curie" is the product-surface name — the CLI binary, the bot handle, the
console. Both names refer to the same system.

## What does Curie do?

Your agent worked on your laptop and then broke once it was deployed. Curie is
a harness that runs the same bundle format and the same `evals/cases.json`
across three targets (`skill` on host Docker, `local` via docker compose, `cluster`
on Kubernetes), so the ladder can surface environment differences before
production when bundle content and grading behavior match. `local`
and `cluster` use stored immutable versioned bundles. `skill` bind mounts the
working bundle directory read only, but host edits can still change it, as
tracked in [issue 1087](https://github.com/curie-eng/curie/issues/1087). A
coding agent can already write a `skill.md`; what it cannot guarantee alone is
that the skill behaves the same deployed as it did locally. That guarantee is
the harness's job: the local loop is the production loop.

A Slack `@mention` (or DM) is answered by a versioned plugin running in an
isolated Kubernetes sandbox, with the run traced end to end and steerable
mid-turn. A `git push` deploys that plugin under a bot identity: push to `dev`
updates `@curie-dev`, merging to `prod` promotes the same built artifact to
`@curie`. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full
component map, a message-flow sequence diagram, and the deploy-flow sequence
diagram.

## Why did my agent work locally but break once deployed?

Because "locally" and "deployed" were different runtimes: a different Python, a
missing tool, an MCP server that resolved on your laptop and not in the cluster,
a credential that was present in one place and absent in the other. Curie
removes that gap by construction. The platform paths run immutable versioned
bundles through the **same runner image** speaking the **same frozen ACI
contract**; only the substrate underneath changes (in process, docker compose,
Kubernetes). The `skill` path currently bind mounts its working directory, so
host edits remain observable under
[issue 1087](https://github.com/curie-eng/curie/issues/1087). With matching
bundle content and grading behavior, a difference between tiers can identify an
environment difference before users hit it. Start from the
[target table](#which-target-do-i-want) and climb `skill` → `local` → `cluster`;
each rung exercises the same bundle format on a heavier substrate, while
`skill` continues to use its working directory.

## How do I test an agent the same way locally and on Kubernetes?

You run the **same `evals/cases.json`** at every tier. `curie skill eval`
grades the bundle in process; the `local` and `cluster` targets drive the same
cases through the real queue → worker → sandbox → runner path a Slack mention
takes, so with matching bundle content and grading behavior, an eval that passes
on your laptop and fails on the cluster can identify a deployment difference.
The case shape and grading
semantics are shared, while `skill` grades in Rust and `local` and `cluster`
grade in Python; conformance fixtures hold those implementations together.
[ADR 0022](docs/adr/0022-eval-completeness-tier-parity-and-trace-promotion.md)
defers a single runner side grader. See
[How do I develop and verify a change?](#how-do-i-develop-and-verify-a-change)
for the per-package verify commands and
[`cli/README.md`](cli/README.md) for the full `eval` reference across targets.

## Status

The core spine is built, covered by CI, and was live-verified end to end
against a real Slack workspace on a real model. For the precise, maintained
built-vs-deferred split, see "What is built vs deferred" in
[`ARCHITECTURE.md`](ARCHITECTURE.md#11-what-is-built-vs-deferred) — this file
does not duplicate that list, which only drifts out of sync.

Forward-looking work is planned and tracked in
[GitHub issues](https://github.com/curie-eng/curie/issues), with larger
journeys filed as `epic`-labeled issues.

## Component map

| Component | Language | What it owns |
|---|---|---|
| [`apps/dispatcher`](apps/dispatcher/README.md) | Python (Slack Bolt) | Socket Mode ingestion: ack, dedupe, placeholder reply, enqueue |
| [`apps/worker`](apps/worker/README.md) | Python (redis-py) | The concurrency kernel (routing, finish-race, steer/interrupt) + the Agent Sandbox substrate |
| [`runner`](runner/README.md) | Python (claude-agent-sdk) | The streaming session server that implements the ACI contract inside a sandbox |
| [`apps/api`](apps/api/README.md) | Python (FastAPI) | Agents/versions/deployments CRUD, plugin bundle pipeline, GitHub git-flow, Langfuse + pod-log proxies |
| [`apps/ui`](apps/ui/README.md) | React (Vite + TS) | The Curie console: author, deploy, and observe agents |
| [`cli`](cli/README.md) | Rust (clap + tokio) | The `curie` CLI: local emulation, evals, deploy, and the cluster operator lifecycle (`up`/`status`/`down`) |
| [`charts/curie`](charts/curie/README.md) | Helm | The umbrella chart: Langfuse + Postgres + Valkey + ClickHouse + MinIO + OTel Collector + the Agent Sandbox substrate, with security rails on by default |
| [`packages/aci-protocol`](packages/aci-protocol/README.md) | Python (frozen, codegen to TS + Rust) | The ACI session protocol every lane speaks |
| [`packages/plugin-format`](packages/plugin-format/README.md) | Python (frozen, codegen to JSON Schema) | The Claude Code plugin bundle shape, verbatim |

## Which target do I want?

Every CLI command that touches an environment takes a **target noun** in the
middle: `skill`, `local`, or `cluster`. Pick the lightest one that answers your
question. (`curie init` is the exception: it scaffolds a bundle on disk and
targets no environment.) The point of the three targets is that the same bundle
format and the same `evals/cases.json` run across all of them, so promoting
`skill` to `local` to `cluster` is a parity ladder, not three separate setups.

| Target | What runs | Slack | Kubernetes | Verbs | Reach for it to |
|---|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | none | none | `up` `down` `status` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `message` `deploy` | Operate and drive a deployed cluster release. |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, and `local`/`cluster` add `deploy`. Parity does not mean
every capability is implemented at every tier: every verb is answered at every
tier, with unsupported concepts returning a deterministic reason and an
alternative, as defined in
[ADR 0041](docs/adr/0041-every-verb-is-answered-at-every-tier.md).

The distinction that matters: `skill` is the **runner-only** loop, it boots just
the runner container and talks straight to its ACI HTTP surface with no platform
in front. `local` and `cluster` put the **full platform** (queue, worker,
sandbox) in front of the identical runner and ACI, so a `message` walks the same
path a real Slack mention would take.

**`skill` (runner-only, fully offline).** `curie skill up` boots the runner
image in Docker (add `--fake-model` for a fully offline round-trip), then
`curie skill message "..."` sends a synthetic Slack event to it and streams
the NDJSON reply to your terminal. `curie skill eval` runs a plugin's
`evals/cases.json` the same way. Abort a live `skill message` with Ctrl-C. This
is the fastest inner loop for developing a plugin: zero Slack, zero platform,
zero cluster. See `cli/README.md`.

**`local` (full platform via compose, no Slack).** `curie local up` brings up
the `full` compose profile by default. `curie local up --minimal` brings up
the smaller `core` profile (API, worker, Postgres, Valkey, MinIO). Then
`curie local deploy` pushes a bundle to the compose API, and
`curie local message "..."` drives a message through the real
queue -> worker -> sandboxed runner -> reply path with no Slack and no
Kubernetes. The compose worker runs the fake model by default, but exporting
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) in your shell is enough for a
real model -- `curie local up` flips to live automatically when a credential is
present, matching `skill up`, so there is no manual `CURIE_FAKE_MODEL=0` step.
See the local runbook below.

**`cluster` (deployed Helm release).** `curie cluster up` installs the platform
on Kubernetes and `curie cluster message "..."` drives the deployed release end
to end with no Slack. See
[How do I run my agent on a Kubernetes cluster?](#how-do-i-run-my-agent-on-a-kubernetes-cluster)
and [`docs/operations.md`](docs/operations.md).

**Real Slack (production).** With a workspace connected, `@mention` the bot in a
channel or DM it: the dispatcher acks, posts a placeholder, and the worker routes
the turn into a claimed sandbox; the placeholder is edited in place as the reply
streams. This is the same platform the `local` and `cluster` targets exercise,
with Slack in front. See `apps/dispatcher/README.md`'s runbook for pointing at a
real workspace.

**Connect a real Slack workspace (local).** The `local` target uses the Slack
stub by default, but you can exercise real Slack routing, mentions and threads
on the compose stack without a cluster. Export the two Slack tokens, empty
`SLACK_API_BASE_URL` to un-wire the worker's Slack stub, and start the optional
dispatcher:

```bash
export SLACK_APP_TOKEN=xapp-... SLACK_BOT_TOKEN=xoxb-...
export SLACK_API_BASE_URL=          # empty un-wires the worker's Slack stub
curie local up --slack

# Raw Docker needs the base profile plus Slack (the dispatcher depends on
# valkey, a core service), for either compose file:
docker compose --profile full --profile slack -f compose.dev.yaml up -d
docker compose --profile full --profile slack -f compose.release.yaml up -d
```

Slack allows exactly one Socket Mode owner per app token at a time, so do not
also run a cluster dispatcher on the same Slack app: it is either/or per app.
Because these are shell exports they persist for the session, so a later plain
`curie local up` in the same shell keeps the worker pointed at real Slack
(empty `SLACK_API_BASE_URL`) with the real bot token but no dispatcher feeding
the queue; open a fresh shell (or `unset SLACK_API_BASE_URL`) to return to the
Slack-free stub. For the full walkthrough (app creation from the manifest,
channel binding, and troubleshooting) see
[`docs/slack-local-runbook.md`](docs/slack-local-runbook.md).

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)**: the Python workspace manager.
- **Python 3.13** (the workspace requires `>=3.13`).
- **Docker + Compose v2**: for the dev stack and the local runner container.
- **Node.js 22 + [pnpm](https://pnpm.io/)**: for the UI.
- **Rust toolchain** (stable, edition 2021, with `rustfmt` and `clippy`): only
  for building the CLI from source. Skip it entirely if you download the
  prebuilt CLI binary from Releases (the default install path below).
- **kubectl + helm**: only for the cluster-install path (see
  [How do I run my agent on a Kubernetes cluster?](#how-do-i-run-my-agent-on-a-kubernetes-cluster)).

## Quickstart

This is the short version — for immediate testing. For a real model, the
offline local-model demo, building from source, and the full local/cluster
runbooks, see the detailed walkthrough in [`QUICKSTART.md`](QUICKSTART.md).

The fastest way in — for operators and coding agents alike — is the prebuilt
release binary. It needs no Rust toolchain and no repo checkout. One command
resolves the latest release, downloads the binary for your platform, verifies
its signed checksum, and installs it to your PATH:

```bash
curl -fsSL https://raw.githubusercontent.com/curie-eng/curie/main/get-curie.sh | bash
```

The installer always verifies the sha256 and runs `cosign verify-blob` when
cosign is on PATH (set `CURIE_REQUIRE_COSIGN=1` to require it). To run every
download-verify-install step by hand instead, or to verify with `gh attestation`,
[`docs/release-verification.md`](docs/release-verification.md#verify-the-cli-before-installing-it)
owns the fully manual flow. Then, from a bundle directory:

```bash
curie init my-agent && cd my-agent
curie skill up --fake-model    # offline scripted model, no credential
curie skill message "hello, are you there?"
curie skill down
```

That is the fastest inner loop: zero Slack, zero platform, zero cluster.

**One-command middle mode** — the same bundle through the real product path
(queue, worker, sandbox), still no host-run worker and no cluster:

```bash
curie local up   # full profile: stores + API + worker + Langfuse + UI
curie local deploy --plugin-dir . --slack-channel C0123ABCD --api-url http://localhost:28000
curie local message "what changed in the last deploy?"
```

Watch it land in the console UI at `http://localhost:28080/?api=1`. `local up`
runs the fake model by default; export `CLAUDE_CODE_OAUTH_TOKEN` or
`ANTHROPIC_API_KEY` beforehand for a real model. A release binary needs no
repo checkout for either `local` or `cluster up` — it pulls the pinned chart
release asset and pinned `compose.release.yaml` matching the binary version,
caching both under `~/.cache/curie/`.

See [`QUICKSTART.md`](QUICKSTART.md) for: a real model in more depth, the
offline `--local-model` demo (Ollama, no Anthropic key), running on a
Kubernetes cluster, the `examples/` bundles, and the contributor path for
building Curie itself from a repo checkout.

## How do I develop and verify a change?

- **Python packages** are one `uv` workspace (root `pyproject.toml`); ruff,
  mypy, and pytest run across all members from the repo root
  (`uv sync && uv run pytest -q && uv run ruff check . && uv run mypy`; see
  [`AGENTS.md`](AGENTS.md) for the full verify command reference). Integration
  tests hit the real dev stack, never mocks of Postgres/Valkey/Langfuse.
- **The Rust CLI** and **the UI** are verified independently; see
  `cli/README.md` and `apps/ui/README.md` for their commands.
- **Work on a feature branch per change**, cut from `main`; never commit to
  `main` directly.
- **Two frozen contracts** (`packages/aci-protocol`, `packages/plugin-format`)
  gate every cross-language lane; changing either requires regenerating the
  committed schema/TS/Rust artifacts and is enforced by a CI compat test.

## How do I run my agent on a Kubernetes cluster?

The same `curie` binary installs and runs the platform on a Kubernetes
cluster, wrapping the umbrella Helm chart the way `linkerd` or `cilium` wrap
theirs. A downloaded release binary resolves the pinned chart release asset for
its version, and `curie local up` likewise resolves the pinned
`compose.release.yaml`, caching both under `~/.cache/curie/` with no repo
checkout needed.
Use `--chart <path>` when developing the chart locally. The short version:

- `curie cluster up` runs `helm upgrade --install` of `charts/curie`; it reads
  `CURIE_CREDENTIALS` (deprecated alias `CURIE_MODEL_CREDENTIALS`) to enable a real model (absent, the release
  installs sealed with canned replies), and `--no-expose` keeps the UI and
  Langfuse ClusterIP-only. The credential alone still leaves the runner sandbox
  sealed against default-deny egress, so a real model stays unreachable until you
  open its provider with `--allow-egress-host <provider>` (currently `anthropic`
  or `openrouter`). `--allow-web-egress <CIDR>` (repeatable) opens runner egress
  for skill web access (e.g. the weather example's live web search), additive to
  the sealed default; `--allow-egress-host` is the model-provider convenience on
  top of it. Connecting Slack is a raw `helm upgrade
  --reuse-values` (not a CLI verb; the chart's `NOTES.txt` prints it).
- `curie cluster status` reports release health and access URLs; `curie cluster down`
  uninstalls and sweeps the runtime namespaces. Every verb takes `--dry-run`.
- `curie cluster message "..."` drives a deployed release end to end with no Slack.

Full runbook (the credential model, the Slack-connect command, and the
zero-Slack `message` flow) is in [`docs/operations.md`](docs/operations.md).

## License and trademarks

Curie is released under the [Apache License 2.0](LICENSE); see [`NOTICE`](NOTICE)
for attribution. "Curie" is a trademark of CurieTech AI. The code license
does not grant trademark rights, and [`TRADEMARKS.md`](TRADEMARKS.md) explains
what use of the name is fine without asking and what needs permission.

If Curie is useful to you, especially if you build on it commercially, we'd
love a link back to [github.com/curie-eng/curie](https://github.com/curie-eng/curie).
It is a friendly request, not a license condition: nothing in the Apache License
requires it, and you are free to use Curie whether or not you do.

## Where do I go next?

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the component diagram, the
  message-flow and deploy-flow sequence diagrams, and the built/in-progress
  split.
- [`docs/adr/`](docs/adr/) — the load-bearing architecture decisions (Agent
  Sandbox as substrate, stateless-first sessions, Langfuse as the
  observability backbone, the frozen ACI, security rails as chart defaults,
  adopt-not-build boundaries), each with the live-cluster evidence behind it.
- [`docs/agents.md`](docs/agents.md): the verification contract for an agent
  driving Curie. The exact commands that prove an outcome, and the rule that a
  file existing or a string appearing in output is never evidence. This is for
  an agent using Curie, not one working in this repo.
- [`AGENTS.md`](AGENTS.md) — the operative rules for anyone (human or agent)
  working in this repo: the verify commands, the dev stack, the
  frozen-contract escalation rule, and the build gotchas. Each top-level
  directory also has its own scoped `CLAUDE.md` with rules specific to that area.
