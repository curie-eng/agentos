# Curie

[![CI](https://github.com/curie-eng/curie/actions/workflows/ci.yaml/badge.svg)](https://github.com/curie-eng/curie/actions/workflows/ci.yaml)

Open-source(Apache 2.0), self-hostable developer platform for Slack-based agents. Connect Slack,
author a Claude-Code-format plugin bundle(skills + tools + MCP), deploy it as a versioned bot
identity and run it anywhere - in your development environment on your laptop or in production
on your own Kubernetes cluster. Configure your model, so you can point an agent at Anthropic,
OpenRouter, or a local model through Ollama. Get traces, evals, budgets, and git-flow deploys
for free. One CLI, `curie`, drives all of it.

New here? [`Quickstart`](#quickstart) gets you a first agent reply in about a minute.

## Why your agent breaks when it leaves your laptop

Local and production environments are usually different - a different Python version, a missing tool,
a credential that exists in one place and not the other. Curie closes that gap with one mechanic:
the same plugin bundle climbs three tiers.

- `skill` runs it directly, as a single container, no platform in front.

- `local` runs it through the full platform via Docker Compose.

- `cluster` runs it through that same full platform on Kubernetes.

An environment difference then shows up as a bug while progressing through these tiers, not a surprise
your users hit - letting you iterate fast locally and ship with confidence.

`local` and `cluster` run immutable versioned bundles; `skill` bind mounts its working directory instead,
so you can edit and iterate fast while building the plugin bundle.

Curie provides an environment guarantee while climbing the three tiers. It is not a behavior
guarantee: production traffic can still behave differently than your test cases, and no platform can
honestly promise otherwise.

See [`ARCHITECTURE.md`](ARCHITECTURE.md#component-map) for the platform architecture and how the pieces fit together.
See the [target table](#which-target-do-i-want) below for what each tier actually runs.

## Quickstart

### Prerequisites

- **Docker + Compose v2**: for the dev stack and the local runner container.
- **kubectl + helm**: only for the cluster-install path.

### Building and deploying your first agent with Curie

Get an [Anthropic API key](https://console.anthropic.com/) and export it once:

```bash
export CURIE_CREDENTIALS=sk-ant-...
```

Every step below reuses this same credential and the same bundle. If you don't have Docker installed,
[install Docker](https://docs.docker.com/get-docker/) and make sure it's running - it is needed for steps 1-2.

**1. Build and test**

```bash
curl -fsSL https://raw.githubusercontent.com/curie-eng/curie/main/get-curie.sh | bash
curie init my-agent && cd my-agent
```

Take a look at what got scaffolded:

```bash
tree -a
```
```
.
├── .mcp.json
├── evals
│   └── cases.json
└── skills
    └── my-agent
        └── SKILL.md
```

- `skills/my-agent/SKILL.md` - the agent's instructions: what it does, and when to use which tool.
- `.mcp.json` - the MCP servers (tools) this agent can call.
- `evals/cases.json` - the eval cases that grade this agent's behavior, at every tier.

This is the Claude Code plugin format, verbatim - see [`packages/plugin-format/README.md`](packages/plugin-format/README.md#format-surface)
for the shape Curie validates against.

```bash
curie skill up
curie skill message "hello, are you there?"
```

A real reply streams back: no Slack, no platform yet. Edit `skills/my-agent/SKILL.md` and re-run to see your
change answered. When done run the following command

```bash
curie skill down
```

This is the fastest inner loop of development and enables you to iterate and build your skills.

**2. Full platform, still your laptop**

Next hook up the agent into the full backend so that the message runs the real queue -> worker -> sandbox -> reply path.

```bash
curie local up
curie local deploy --plugin-dir . --slack-channel C0123ABCD --api-url http://localhost:28000
curie local message "hello, are you there?"
```

Then continue this conversation thread

```bash
curie local message --continue "what's 2 + 2?"
```

This is the same path a real Slack `@mention`
takes - see [`docs/slack-local-runbook.md`](docs/slack-local-runbook.md) when you're ready to try it live.

Visit the console at 

```bash
http://localhost:28080/?api=1
```

to see the whole conversation, its traces, metrics, and cost. The same console also surfaces logs,
approvals, and memory, which get more relevant once this plugin is deployed on Kubernetes in production.

When done run the following command

```bash
curie local down
```

**3. Real Kubernetes**

Finally, deploy the bundle on Kubernetes.
Point `kubectl`/`helm` at a cluster - k3s is the lasting recommendation; if you don't have a cluster
handy, [install minikube](https://minikube.sigs.k8s.io/) and run the following command
(See [`docs/operations.md`](docs/operations.md#the-kubernetes-cluster) for the tradeoffs between k3s and minikube):

```bash
minikube start
```

Then:

```bash
curie cluster up --allow-egress-host anthropic --set security.gvisor.mode=off
curie cluster deploy --plugin-dir .
curie cluster message "hello, are you there?"
```

`--set security.gvisor.mode=off` skips gVisor's extra kernel isolation, which a real-model install
otherwise requires and minikube doesn't ship by default - drop it on a cluster that has `runsc`
installed. 

`--allow-egress-host` opens the model call; a credential alone doesn't, since the cluster sandbox is
fail-closed by default (skill/local aren't).

See [`docs/operations.md`](docs/operations.md#installing-and-inspecting-the-curie-platform-on-the-cluster) for cluster prerequisites and the full egress model.

Then continue this conversation thread

```bash
curie cluster message --continue "what's 2 + 2?"
```

When done run the following command

```bash
curie cluster down --yes
```

**4. Ship it: your CI/CD**

Connect this bundle's repo once (see [`docs/operations.md`](docs/operations.md#automatically-with-git-flow)), then:

```bash
git push origin dev
```

Every push is stored as an immutable, versioned bundle and deployed under your `dev` bot
automatically. Merging to prod promotes that same version, not a rebuild, so you always
know exactly what's live and can roll back to any version.

No Slack event loop, no queue, no sandbox plumbing to write. You asked the same bundle
to run somewhere bigger, then told it to ship itself.

Ready to make it real? See [`docs/slack-local-runbook.md`](docs/slack-local-runbook.md) to wire
this bundle into an actual Slack workspace.

Once this is live in `dev` or `prod`, `@mention` the bot in Slack like any teammate - see
[`apps/dispatcher/README.md`](apps/dispatcher/README.md#runbook-point-it-at-a-real-slack-workspace-once-one-exists)'s runbook for connecting a real workspace to a deployed release.

See [`QUICKSTART.md`](QUICKSTART.md) for the offline `--fake-model` path,
the `examples/` bundles, and building Curie from source.

## Which target do I want?

Every CLI command that touches an environment takes a **target noun** in the
middle: `skill`, `local`, or `cluster`. Pick the lightest one that answers your
question.`curie init` is the exception: it scaffolds a plugin bundle on disk and
targets no environment. The point of the three targets is that the same
plugin bundle format and the same `evals/cases.json` run across all of them, so
promoting `skill` → `local` → `cluster` is a parity ladder, not three separate
setups. An eval that passes on your laptop and fails on the cluster is
signal, not noise; each target's `eval` command is documented alongside it
in [`cli/README.md`](cli/README.md).

| Target    | What runs                                                                                                    | Slack    | Kubernetes | Verbs                                   | Reach for it to                                                                                                                                    |
| --------- | ------------------------------------------------------------------------------------------------------------ | -------- | ---------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `skill`   | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none     | none       | `up` `down` `status` `message` `eval`   | Iterate a plugin/skill against a local runner, the fastest development loop.                                                                                   |
| `local`   | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker).                          | none     | none       | `up` `down` `status` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release).                                                                 | optional | yes        | `up` `down` `status` `message` `deploy` | Operate and drive a deployed cluster release.                                                                                                      |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, and `local`/`cluster` add `deploy`. Parity does not mean
every capability is implemented at every tier: every verb is answered at every
tier, with unsupported concepts returning a deterministic reason and an
alternative, as defined in
[ADR 0041](docs/adr/0041-every-verb-is-answered-at-every-tier.md).

The distinction that matters: `skill` is the **runner-only** loop — it boots
just the runner container and talks straight to its ACI HTTP surface with no
platform in front. `local` and `cluster` put the **full platform** (queue,
worker, sandbox) in front of the identical runner and ACI. A `message` on
either therefore walks the same path a real Slack mention would take.

See `cli/README.md` for the full command reference per target
([`skill`](cli/README.md#skill-target),
[`local`](cli/README.md#local-target),
[`cluster`](cli/README.md#cluster-target)),
[`docs/slack-local-runbook.md`](docs/slack-local-runbook.md) for connecting local Slack, and
[`docs/operations.md`](docs/operations.md) for cluster operations - the Quickstart above already
walks through `skill`, `local`, and `cluster` end to end.

## Status

The core spine is built, covered by CI, and was live-verified end to end
against a real Slack workspace on a real model. For the precise, maintained
built-vs-deferred split, see "What is built vs deferred" in
[`ARCHITECTURE.md`](ARCHITECTURE.md#what-is-built-vs-deferred) — this file
does not duplicate that list, which only drifts out of sync.

Forward-looking work is planned and tracked in
[GitHub issues](https://github.com/curie-eng/curie/issues), with larger
journeys filed as `epic`-labeled issues.

## Contributing to Curie
See [`CONTRIBUTING.md`](CONTRIBUTING.md#development-setup) for the full contributor setup (uv, Python
3.13, Node.js + pnpm, Rust toolchain) and verify commands.

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

- [`ARCHITECTURE.md`](ARCHITECTURE.md#component-map) -- the component diagram, the
message-flow and deploy-flow sequence diagrams, and the built/in-progress
split.
- [docs/adr/](docs/adr/) -- the load-bearing architecture decisions (Agent
Sandbox as substrate, stateless-first sessions, Langfuse as the
observability backbone, the frozen ACI, security rails as chart defaults,
adopt-not-build boundaries), each with the live-cluster evidence behind it.
- [docs/agents.md](docs/agents.md): the verification contract for an agent
driving Curie. The exact commands that prove an outcome, and the rule that a
file existing or a string appearing in output is never evidence. This is for
an agent using Curie, not one working in this repo.
- [`AGENTS.md`](AGENTS.md) -- the operative rules for anyone (human or agent)
working in this repo: the verify commands, the dev stack, the
frozen-contract escalation rule, and the build gotchas. Each top-level
directory also has its own scoped `CLAUDE.md` with rules specific to that area.

