# Contributing to Curie

Thanks for your interest in contributing. This guide covers how to prepare a
change that fits this repository's conventions, how to run the checks CI runs,
and how contributions are certified.

Read [`AGENTS.md`](AGENTS.md) alongside this guide: it is the authoritative
source for the build, test, and architecture conventions summarized here, and
each area's own `CLAUDE.md` carries the rules specific to that directory.

<!-- TODO(maintainer): Decision 1 (issue #635) - confirm whether outside pull
     requests are accepted and what scope is welcome. This guide is written
     assuming community PRs are welcome. If that holds, delete this comment and
     the placeholder scope note below and state the accepted scope plainly
     (for example: bug fixes and documentation freely; new connectors, CLI
     verbs, or UI surfaces via an issue first; changes to the frozen contracts
     never as a drive-by). If outside PRs are NOT accepted, say so here instead
     and describe how to propose changes. -->

## Scope of contributions

We welcome bug reports, documentation improvements, tests, and focused feature
work. Before starting anything larger than a bug fix or a docs tweak, please
open a GitHub issue describing the change so a maintainer can confirm it fits
the project's direction. This is especially important for anything that touches
a cross-component seam or a frozen contract (see below).

> TODO(maintainer): confirm the accepted contribution scope for the public
> announcement, then finalize the paragraph above.

## Before you start

- **Be respectful.** All participation is governed by our
  [Code of Conduct](CODE_OF_CONDUCT.md).
- **Security issues do not go here.** Never open a public issue or PR for a
  vulnerability. Follow [`SECURITY.md`](SECURITY.md) for private disclosure.
- **Questions and usage help** belong in the support channels described in
  [`SUPPORT.md`](SUPPORT.md), not in a pull request.

## Development setup

Curie is a multi-language monorepo: a Python
[uv](https://docs.astral.sh/uv/) workspace (the platform services and packages),
a Rust CLI, and a React (Vite + TS) UI, orchestrated over a Docker Compose dev
stack. See [`README.md`](README.md) and [`QUICKSTART.md`](QUICKSTART.md) for the
product overview and the fastest path to a running system, and
[`ARCHITECTURE.md`](ARCHITECTURE.md) before touching a cross-component seam.

Bring up the backing stack and install the workspace:

```bash
# Backing stack (Postgres + Valkey + Langfuse v3 + ClickHouse + MinIO + OTel).
# Runs on baked defaults; copy .env.example to the gitignored .env only to override.
cp .env.example .env    # optional
docker compose --profile full -f compose.dev.yaml up -d
docker compose -f compose.dev.yaml ps    # wait for all services healthy

# Python workspace
uv sync
```

Bring the stack down when you are done: `docker compose -f compose.dev.yaml
down` (add `-v` to wipe volumes). If you brought it up, you own tearing it
down. AGENTS.md documents the per-service host ports and the load-bearing
gotchas.

Do stack testing from a git worktree cut from `origin/main`, not the main
checkout. Read-only runs against the current tree are fine; the moment you need
to change code, cut a worktree.

## Running the checks

CI (`.github/workflows/ci.yaml`) runs the same commands below. Run the ones for
the area you touched before opening a PR. Scope test runs to what you changed;
CI covers the rest.

**Python (all packages, from the repo root):**

```bash
uv sync                 # once, and after any dependency change
uv run pytest -q        # workspace tests
uv run ruff check .     # lint (auto-fix: uv run ruff check --fix .)
uv run mypy             # type-check (strict)
```

**Rust CLI:**

```bash
cd cli
cargo fmt --check
cargo clippy -- -D warnings
cargo test
```

If `cargo fmt`/`clippy` report a missing component: `rustup component add
rustfmt clippy`.

**UI:**

```bash
cd apps/ui
pnpm install && pnpm lint && pnpm typecheck && pnpm test && pnpm e2e
```

**Docs (interface catalog):** if you edit any doc under `docs/`, including the
agent verification contract (`docs/agents.md`), regenerate and lint it with
`bash scripts/check-docs.sh` (the local mirror of the CI docs gate) and commit
the regenerated files.

### Testing discipline

- Test-first for behavior-bearing code. Mock only external services (Slack,
  Anthropic, GitHub); never mock Postgres, Valkey, or Langfuse: run integration
  tests against the dev stack above.
- Assert real outcomes (values, state transitions, emitted events), not
  presence. A change that passes only by weakening assertions is a regression.
- Every behavior-bearing change must be verified end-to-end through the real
  product loop (the `curie` CLI, the compose services, or a real sandbox on a
  local `kind`/`k3s` cluster), not just by unit tests.
- If you touch one side of a known parity seam (see the registry in AGENTS.md),
  change the sibling in the same PR, route both through a shared helper, or name
  the sibling in the PR body with a follow-up issue.

## Frozen contracts: stop and escalate

`packages/aci-protocol` and `packages/plugin-format` are frozen interfaces that
every lane compiles against across three languages. If your change needs to
modify either package, stop: do not work around it. Open an issue or raise it in
your PR. A contract change must land as its own reviewed, backward-compatible
change first, before dependent lanes proceed. The same applies when an adopted
component cannot do what a spec claims: stop and raise it with evidence rather
than silently diverging.

## Decisions: ADR vs GitHub issue

Two different tools; do not conflate them.

- Write an **ADR** (`docs/adr/`, Michael Nygard style, see ADR-0001) only for a
  cross-cutting architectural decision that closes the door on alternatives. An
  ADR must record what was decided against and why. ADRs are immutable once
  Accepted; to change a decision, add a new ADR that supersedes the old one.
- Write a **GitHub issue** for a feature, however large. A new CLI command, a UI
  surface, or a connector is deletable and does not change the architecture, so
  it is a feature, not an architectural decision. The issue carries the what and
  the why; the how lives in the PR.
- When in doubt, write the issue.

## Branch, commit, and PR conventions

- **Branch per change**, cut from the latest `origin/main`, named
  `task/<short-description>`. Keep the slug terse. Never commit to `main`.
- **Commit messages**: a short imperative summary line, then detail bullets.
- **Reference the issue in the PR body** with `Closes #123` (or `Ref #123`). Do
  not put the issue number in the PR or commit title; use a plain descriptive
  title.
- **No dashes or emdashes in prose; no emojis** in code or docs.
- **Never mention any AI assistant** (or AI in general) in commit messages, and
  never add `Co-Authored-By` lines referencing an AI.
- Open a PR against `main`. Keep the change focused; a PR that touches unrelated
  areas is harder to review and to revert.

## Certifying your contribution

<!-- TODO(maintainer): Decision 3 (issue #635) - confirm DCO vs CLA. This guide
     recommends the Developer Certificate of Origin (DCO) as the lighter-weight,
     lower-friction default: no paperwork, no per-contributor legal review, just
     a per-commit sign-off. If the org instead requires a Contributor License
     Agreement (CLA), replace this section with the CLA instructions and bot,
     and remove the DCO sign-off requirement. -->

This project uses the **Developer Certificate of Origin (DCO)**. The DCO is a
lightweight, per-commit affirmation that you wrote the change or otherwise have
the right to submit it under the project's license. It is the recommended
default here because it adds no paperwork: you certify each contribution by
signing off on your commits.

> TODO(maintainer): confirm DCO vs CLA for the public launch (issue #635,
> decision 3). If DCO stands, wire up a DCO check on pull requests.

To sign off, add a `Signed-off-by` line to each commit. Git does this for you
with the `-s` flag:

```bash
git commit -s -m "Fix the thing"
```

This appends a line using your configured `user.name` and `user.email`:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Use your real name and a reachable email. By signing off you certify the
Developer Certificate of Origin, version 1.1, reproduced in full below.

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

If you forgot to sign off, amend the most recent commit with `git commit
--amend -s`, or for a range use `git rebase --signoff <base>`.

Note on licensing: the project's open source license is a separate decision
tracked outside this guide. The DCO certifies your right to submit under
whatever license the project adopts; it is not itself a license grant.

## Getting help while contributing

If you get stuck, see [`SUPPORT.md`](SUPPORT.md) for where to ask. Thank you for
contributing to Curie.
