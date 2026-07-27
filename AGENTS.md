# AGENTS.md - Curie

Agent instructions for this repo. Start with [`README.md`](README.md) for what
the product is and how to run it. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for:

- The component diagram.
- The message-flow sequence (Slack mention -> dispatcher -> worker -> sandbox
  -> runner -> Slack reply).
- The deploy-flow sequence (git push -> webhook -> bundle pipeline ->
  deployment).

Read `ARCHITECTURE.md` before touching a cross-component seam. If you are a
coding agent orienting in this repo, [`llms.txt`](llms.txt) is the curated
machine map of these docs, organized around the parity ladder.

The one-line version: a Slack message is answered by a versioned plugin running
in an isolated Kubernetes sandbox, traced through Langfuse and steerable
mid-turn; a git push deploys that plugin under a bot identity via the API's
git-flow engine.

_(Relay is the project codename; `curie` is the product-surface name -- the bot
handle and CLI binary.)_

The two questions this repo exists to answer:

- [Why did my agent work locally but break once deployed?](README.md#why-did-my-agent-work-locally-but-break-once-deployed)
- [How do I test an agent the same way locally and on Kubernetes?](README.md#how-do-i-test-an-agent-the-same-way-locally-and-on-kubernetes)

The same immutable bundle and the same `evals/cases.json` across `skill`,
`local`, and `cluster` is the answer to both.

## Directory map

One directory is one ownership boundary. Each area's own `CLAUDE.md` (linked
below) carries the rules and verify commands specific to that area -- read it
before editing there, in addition to this file.

| Path | Language | Scoped rules |
|---|---|---|
| `packages/aci-protocol` | Python (Pydantic + codegen) | [`packages/CLAUDE.md`](packages/CLAUDE.md) |
| `packages/plugin-format` | Python (Pydantic + codegen) | [`packages/CLAUDE.md`](packages/CLAUDE.md) |
| `apps/api` | Python (FastAPI) | [`apps/api/CLAUDE.md`](apps/api/CLAUDE.md) |
| `apps/dispatcher` | Python (Slack Bolt) | [`apps/dispatcher/CLAUDE.md`](apps/dispatcher/CLAUDE.md) |
| `apps/worker` | Python (redis-py) | [`apps/worker/CLAUDE.md`](apps/worker/CLAUDE.md) |
| `runner` | Python (claude-agent-sdk) | [`runner/CLAUDE.md`](runner/CLAUDE.md) |
| `apps/ui` | React (Vite + TS) | [`apps/ui/CLAUDE.md`](apps/ui/CLAUDE.md) |
| `cli` | Rust (clap + tokio) | [`cli/CLAUDE.md`](cli/CLAUDE.md) |
| `charts/curie` | Helm | [`charts/curie/CLAUDE.md`](charts/curie/CLAUDE.md) |
| `tests/soak` | Python | -- |

The Python packages are one **uv workspace** (root `pyproject.toml`); ruff,
mypy, and pytest are configured at the root and run across all members.

## Verify commands (per package)

Run these from the repo root unless noted. CI (`.github/workflows/ci.yaml`) runs
the same commands.

**Python (all packages, from root):**
```bash
uv sync                 # once, and after any dependency change
uv run pytest -q        # all workspace tests
uv run ruff check .     # lint (auto-fix: uv run ruff check --fix .)
uv run mypy             # type-check (strict; targets the src dirs)
```

**Rust CLI:**
```bash
cd cli
cargo fmt --check         # formatting
cargo clippy -- -D warnings   # lint, warnings as errors
cargo test                # unit + integration tests
```
If `cargo fmt`/`clippy` report a missing component: `rustup component add rustfmt clippy`.

**UI:** the app is a real Vite + React + TS project -- see `apps/ui/CLAUDE.md`.
```bash
cd apps/ui
pnpm install             # once
pnpm lint                # eslint
pnpm typecheck           # tsc project check
pnpm test                # unit tests
pnpm e2e                 # Playwright e2e
```
The top-level CI workflow's `ui` job runs the full pnpm lint, vitest, build, and
stackless Playwright suite; run `pnpm test`/`pnpm e2e` locally to match it.

**Docs (interface catalog):** `curie dev docs-lint` regenerates the seam index
(`docs/interfaces.md`), each `INTERFACE.md` header, and the ADR (Architecture
Decision Record) index (`docs/adr/README.md`) from source. It then checks:

- No doc under `docs/` (excluding `docs/adr/`, whose citations are immutable
  history) or the repo-root docs on its allowlist (currently `ARCHITECTURE.md`)
  carries a line-number citation.
- Every cited path resolves.
- Every cited Python symbol resolves.
- Each graded seam's `grade:` agrees with the row its `vision_row:` names in
  `docs/architecture-vision.md`.
- No two ADRs under `docs/adr/` claim the same number prefix.

Run it after editing any interface-catalog doc and commit the regenerated
files; CI runs the same check (`scripts/check-docs.sh`) in the `python` job. To
exempt a genuinely illustrative example path, put
`<!-- doclint:ignore-line -->` on that line (or the line before it).

Test discipline: test-first for behavior-bearing code; mock ONLY external
services (Slack, Anthropic, GitHub); NEVER mock Postgres/Valkey/Langfuse -- run
integration tests against the dev stack below. A change that only makes tests
pass by weakening assertions is a regression. At parity seams, include at least
one negative or secondary-path test per AC (acceptance criterion; see the
parity-seam registry). Assertions about an external API or SDK's shape or auth
must be grounded in provider docs or observed behavior, cited in a test
comment, never in the implementation's own assumption. Any read-modify-write on
a versioned row needs a stale-version conflict test (match the CAS
(compare-and-swap) pattern in `apps/api/src/curie_api/routers/state.py`).
Every stream consumer lane derives bounded delivery + dead-letter from the
shared transport; a lane without a delivery cap is a bug.

## The dev stack: compose.dev.yaml

The compose stack now has two profiles. `full` brings up the whole backing
stack (Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel Collector).
`core` brings up the smaller local product loop (Postgres + Valkey + MinIO +
API + worker). Every backend integration test and UI E2E runs against `full`.

```bash
docker compose --profile full -f compose.dev.yaml up -d   # full stack
OTEL_EXPORTER_OTLP_ENDPOINT= docker compose --profile core -f compose.dev.yaml up -d   # 7-service minimal stack (no Langfuse/ClickHouse/OTel/UI); blank endpoint avoids a DNS retry against the absent otel-collector
docker compose -f compose.dev.yaml ps        # check health
docker compose -f compose.dev.yaml down      # stop, KEEP volumes (fast restart)
docker compose -f compose.dev.yaml down -v   # stop and WIPE volumes (throwaway)
```

**Clean up after yourself — this is not optional.** If you bring the local
stack up, you MUST take it down when you are done. This box does not have the
RAM to leave the full stack idling, and this keeps happening: stacks get left
running across sessions and starve the machine. Before you end a session in
which you ran `curie local up` / `docker compose ... up`, run `curie local
down` (or `docker compose -f compose.dev.yaml down`). Confirm with `docker ps`
that nothing curie-related is still up, and remove any stray `curie-runner*`
containers a run may have spawned. The thread that brought the stack up owns
tearing it down. A blocked or crashed test agent never cleans up after itself
-- do not assume someone else will.

**Do stack testing from a worktree, not the main checkout.** If a local test
requires code edits, make them in a git worktree cut from `origin/main` and
land them as a PR. Never edit `main` in place to make a local run work.
Read-only runs against the current tree are fine; the moment you need to
change code, cut a worktree.

Add `--profile slack` through `curie local up --slack` to start the optional dispatcher for real Slack.

Host ports (non-default host ports to avoid local collisions):

| Service | Host port |
|---|---|
| Langfuse UI | http://localhost:23000 |
| Postgres | localhost:25432 |
| Valkey | localhost:26379 |
| ClickHouse | HTTP 28123, native 29009 |
| MinIO | S3 29000, console 29001 |
| OTel Collector | gRPC 24317, HTTP 24318 |

Config lives in `.env.example` (copy to the gitignored `.env` to override; the
stack runs on the baked defaults without one). Load-bearing facts:

- **ClickHouse is pinned to `:24.8`.** Newer ClickHouse requires AVX (a CPU
  instruction-set extension); without it, a CPU raises SIGILL (the
  illegal-instruction signal) and the process exits 132. Keep the pin unless
  every target CPU has AVX. `charts/curie` turns this into a chart preflight
  (`preflights.avxCheck`).
- **Langfuse OTLP (OpenTelemetry Protocol) ingest is HTTP-only** (gRPC is
  silently unsupported). Services may emit OTLP over gRPC or HTTP to the OTel
  Collector (4317/4318); the collector always exports to Langfuse over HTTP.
  Send app traces to the collector, not directly to Langfuse.
- **Langfuse is bootstrapped headless** with a fixed dev project (`curie-dev`)
  and keys `pk-lf-curie-dev` / `sk-lf-curie-dev`, so the OTel path
  authenticates on first boot with no manual key-minting. Read traces back via
  `curl -u pk-lf-curie-dev:sk-lf-curie-dev http://localhost:23000/api/public/...`. <!-- gitleaks:allow -->

## Frozen contracts: STOP and escalate

`packages/aci-protocol` (the ACI session protocol + NDJSON events) and
`packages/plugin-format` (the Claude Code plugin shape, verbatim) are **frozen
interfaces**. Every lane compiles against them across three languages (Pydantic
source of truth -> committed JSON Schema -> generated TS + Rust).

Two CI gates guard the ACI, and neither infers backward compatibility on its
own:

- The **schema-compat test** catches **artifact-sync drift** -- a model change
  that was not regenerated into the committed schema/TS/Rust.
- The **wire-lock gate** (`packages/aci-protocol/schema/wire.lock`) fails a
  wire change that did not bump `PROTOCOL_VERSION`, telling you which bump to
  make.

Backward compatibility itself is a **policy** the semver table in
`packages/CLAUDE.md` defines and a human applies per change class. CI
enforces that you version the change, not that the change is safe.

If your task needs a change to either package: **stop, do not work around it,
and open a GitHub issue or raise it in your PR.** A contract change must land
as its own reviewed, backward-compatible change first, before dependent lanes
proceed. This also applies whenever an adopted component (Langfuse, Agent
Sandbox, Bolt) cannot do what a spec claims: stop and raise it with the
evidence rather than silently diverging.

## Parity seams: cover the sibling or file it

Sibling-path drift is the dominant historical bug class here: logic or hardening
lands on one side of a structural seam while its twin keeps the old behavior.
Known seam pairs:

| Seam | Side A | Side B | Notes |
|---|---|---|---|
| Credential forwarding | `_SDK_PASSTHROUGH_ENV` (`apps/worker/src/curie_worker/sandbox/docker.py`) | The CLI credential picker (`cli/src/commands.rs`) | Can't share code across Python/Rust; frozen together in `tests/vectors/model-credential-forwarding.json`. |
| Model session | The real SDK session in the runner | `FakeModelSession` (`runner/src/curie_runner/fake.py`) | |
| Stream consumers | The runs-lane consumer (`apps/worker/src/curie_worker/consumer.py`) | The eval-lane consumer (`eval/stream.py`) | Both built on the shared `stream_consumer.py`. |
| Input validation | CLI-side validation | API-side validation | Validate at the API/persistence boundary, mirror in the CLI. |
| Compose profile sets | `local up` | `local down` | |
| Compose definitions | `compose.dev.yaml` | The generated release compose | |
| Compose profiles | `core` | `full` | |
| Verb pairs | `local` | `cluster` | Reachability defaults, outcome enums. |
| Output shape | CLI `--json` DTOs (data transfer objects) | The API models they mirror | |
| Value parsing | Deploy-time validators | The runtime loaders that re-parse the same value | Share normalization code. |

A PR touching one side of a seam must do one of:

- Route the behavior through a shared helper both sides call.
- Change both sides in the same PR.
- Name the sibling in the PR body with a follow-up issue number.

Prefer parity by construction over remembered duplication. Ship a test that
arms the behavior via the secondary path only (bundle-manifest-only gate,
fake-tier-only, minimal-profile-only) and asserts it matches the primary path.

## Guards are outcome-tested

A new or modified gate, validator, denylist, or preflight lands only with a
demonstration that it rejects a violating input by execution, not by reading the
code. Its regression test asserts the outcome through the real consumer path (the
filter the user hits, the loader that re-parses the value), never an internal
struct field. No doc or comment may claim a protection that no code realizes -- a
claimed-but-absent guard is worse than none.

## E2E verification is mandatory

Almost everything here is end-to-end testable, and the CLI makes it cheap.
Local skills, the compose dev stack, and a disposable local k8s cluster
(kind/k3s) let you exercise a change against the real product loop, not a
mock. So every behavior-bearing change must be verified end-to-end before it
is called done. Drive the actual surface (the `curie` CLI, the deployed
compose services, a real sandbox on-cluster) with realistic input and assert
the real outcome, not just that unit tests pass.

- **In-repo tests are the durable net.** Prefer landing unit + integration tests
  (and a Playwright/e2e assertion where a UI or full-flow path changed) in the
  same PR. These are what keep the change working after you leave.
- **A hands-on e2e pass is non-negotiable on top of that.** Even when CI is
  green, run the changed path yourself through the CLI / docker / cluster and
  confirm the observable behavior. CI runs against frozen fixtures; a live pass
  catches config drift, deploy-pipeline regressions, and "is my code path even
  wired" gaps that unit tests cannot see.
- **Assert outcomes, not presence.** Use strong, deterministic assertions on real
  behavior (values, state transitions, emitted events, trace contents). Avoid
  hollow "does it render / does an element exist" checks and any AI-vision or
  screenshot-polling assertions -- they mask weak architecture and rot fast.
- **New/changed CLI commands follow the agent-facing contract (ADR-0021).**
  Every new or changed command must provide:
  - Structured `--json` output for read/report commands (JSON to stdout,
    human/log to stderr).
  - Semantic exit codes (0 success / 1 failure / 2 usage / 3 transient).
  - Non-interactive operation (a `--yes`/`--force` path, never blocking on
    stdin).
  - Errors as `{"error","fix"}` recovery instructions.

  Exit-code scheme: see `cli/README.md`.
- **The agent-facing read and result verbs emit one JSON object to stdout
  under `--json` -- never empty stdout (issue #456).** This covers the
  read/query verbs (`versions`, `memory`, `approvals`, `observability`), the
  lifecycle result verbs (`kill`, `resume`, `budget`, `delete`), and every
  verb's `--dry-run` plan. Silent empty-stdout-exit-0 is the worst failure for
  an agent consumer: it reads as success while carrying no data.

  To apply: a new or refactored verb returns a `CliOutput` (a typed output
  object, or `DryRunPlan` for a `--dry-run` plan). It routes that through
  `Ui::emit`, the single place the json-vs-human decision is made -- handlers
  do not call stdout emitters (`payload`/`kv`/`payload_plain`) directly.

  Two tracked exceptions:
  - The schema-gated ADR-0021 builders (`skill status`/`skill eval`, `skill
    check`, `local message`/`cluster message`, `secrets list`, the error path,
    `guide`) inline the same `if json` decision themselves. This is
    sanctioned and tracked for migration onto `Ui::emit` in #474.
  - The operator verbs (`up`, `down`, `status`, `comms`), `deploy`, and `skill
    message` have real-path success output that is not yet structured under
    `--json`, tracked in #485.
- **Console/CLI parity is a two-sided invariant (epic #145).** Any CLI
  command-surface change regenerates the committed manifest (`cli/CLAUDE.md`),
  and every wired console action maps to a real command or an explicit
  `noCliEquivalent` (`apps/ui/CLAUDE.md`). Keep both sides in the same change.
- **A runtime acceptance criterion is not satisfied by static verification.**
  When a ticket's AC is a runtime/observable check -- an exec command, an HTTP
  response, a rendered-then-running behavior -- you MUST run that exact check
  against a running cluster. Paste its output: `helm lint`, `helm template`,
  typecheck, and render do NOT count. Why: static checks never run the init
  container or the live binary, so they cannot see the behavior the AC is
  about. (#56, a credential-isolation bug in the bundle-fetch init container,
  was nearly shipped green on lint + template alone.) How to apply: for chart
  / sandbox / bundle changes, use `curie dev chart-runtime-e2e` (implemented
  by `scripts/chart-runtime-e2e.sh`). It is the one-command way to install a
  trimmed slice, run the init containers, and exec-assert.

## Playwright: two modes

- **The merge gate is the committed E2E suite** under `apps/ui` (Playwright,
  headless, in CI against the compose stack). It asserts behavior (deploy flow
  completes, runs view renders the tool-call tree, eval matrix populates). This
  is the regression net; it must be green to merge.
- **The `@playwright/mcp` server** (wired in `.mcp.json`) is for interactive
  verification *during* development. Drive the real browser, click through the
  flow you just built, and screenshot it to check visual fidelity. Commit
  assertions into the suite to make them a gate.

## Cluster verification

Chart, sandbox, and soak verification need a real cluster; a disposable local
`kind` or `k3s` cluster works. The cheap default for a chart/sandbox/bundle
change is `curie dev chart-runtime-e2e` (implemented by
`scripts/chart-runtime-e2e.sh`). It installs a trimmed slice, runs the
bundle-fetch init pair, and exec-asserts on the runner -- the one-command way
to satisfy a runtime AC. See
[`charts/curie/CLAUDE.md`](charts/curie/CLAUDE.md) for the install and probe
commands.

## Branch and commit conventions

- Branch per change: `task/<short-description>`, cut from the latest
  `origin/main`. Never commit to `main`.
- Commit message format: a short imperative summary line, then detail bullets.
- Reference the relevant issue in the PR body (e.g. `Closes #123`).
- **Never mention any AI assistant (Claude, Codex, GPT, etc.) or AI in general in
  commit messages, and never add `Co-Authored-By` lines referencing AI.**
- No dashes/emdashes in prose content; no emojis in code or docs.

## Decisions: ADR vs. GitHub issue

Two different tools; do not conflate them.

- Write an **ADR** (`docs/adr/`, see ADR-0001) only for a **cross-cutting
  architectural decision that closes the door on alternatives.** It is a choice
  about the *shape* of the system (a contract, a seam, a substrate, an
  invariant) that is expensive to reverse. A future contributor must
  understand its *why* before touching that area. An ADR is not just what we
  chose; it **must record what we decided against and why** (the alternatives
  and their rejection). If no real alternative is being closed off, it is not
  an ADR.
- An ADR or plan whose rationale claims observability, convergence, protection,
  or parity must name the code path that realizes the claim in the same
  change. If it can't yet, it must name the tracked follow-up issue that
  will. A rationale that says "this becomes observable" without a consumer or
  alert is an unmet claim and blocks acceptance.
- Write a **GitHub issue** (with a rich description) for a **feature**, however
  large. A new CLI command, a UI surface, a connector -- it may be a lot of
  code, but it is deletable and does not change the architecture. That makes
  it a feature, not an architectural decision. The issue carries the what and
  the why; the *how* lives in the PR. An issue may cite an ADR.
- **When in doubt, write the issue.** Promote to an ADR only when the same decision
  gets re-explained across a third issue or PR.

## Gotchas discovered during the build

- **Deployment-to-runtime binding is wired; it binds per fresh mention.** The
  worker resolves a thread's Slack channel to its agent, that agent's active
  deployment (prod outranks dev, then most recent), and the resolved
  `CURIE_BUNDLE_REF`, injecting it into each sandbox claim. A fresh mention
  boots the exact bundle version the API's git-flow engine produced
  (`apps/worker/src/curie_worker/binding.py`). The seam to remember: an
  existing thread keeps the sandbox and bundle it first booted with. Only a
  new mention (a new claim) picks up a newer deployment.
- **Sandbox cold boots must never pull an image.** The four Deployment
  services (`api`, `worker`, `dispatcher`, `ui`) default to `pullPolicy:
  Always`. The runner image, though, is `IfNotPresent` and kept pinned on
  every node by the `agentSandbox.runner.prewarm` DaemonSet, which pulls at
  install/upgrade. A mid-boot pull of the ~380MB runner image blew the 90s
  claim timeout in a live incident (2026-07-06). Never switch the runner
  image to `Always` (`charts/curie/templates/runner-prewarm.yaml`,
  `charts/curie/values.yaml`).
- **Suspend/resume is a cold rehydrate, not a live hibernate** (ADR-0003). A
  suspended sandbox's pod is deleted; resume creates a new pod and injects
  `CURIE_HISTORY_REF`. Never assume prompt-cache warmth survives a suspend, and
  never design a feature that needs a sandbox's in-process state to outlive a
  suspend.
- **Warm-pool claims are fast only without per-claim env.** A claim that needs
  `CURIE_HISTORY_REF`/`CURIE_SESSION_ID` injected (the resume path) cannot
  bind a pre-warmed sandbox and cold-creates one instead (seconds, not the ~0.2s
  warm-pool bind). This is inherent to `agent-sandbox`'s
  `envVarsInjectionPolicy: Overrides`, not a bug to fix.
- **A cluster's CNI (Container Network Interface) must actually enforce
  NetworkPolicy** or the chart's egress lockdown is a silent false-pass. The
  chart ships a before/after enforcement probe (`preflights.networkPolicyProbe`)
  for exactly this reason -- never trust an egress policy without it.
- **gVisor needs `runsc` on the node**, which the chart cannot install. On a
  cluster without it, use the ready-made `-f charts/curie/values-e2e-nogvisor.yaml`
  overlay rather than hand-editing security values (see `charts/curie/CLAUDE.md`).
