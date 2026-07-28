# 84. Publish a verification contract for agents driving Curie

Date: 2026-07-28

Status: Accepted

## Context

Curie's CLI says its primary user is a coding agent driven by a developer, but
nothing published told that agent what counts as proof. The observed failure is
consistent: an agent reports an outcome achieved on **textual** evidence, a file
appearing on disk, a string appearing in output, a command not erroring, none of
which establishes that the thing under test actually works. `curie guide` gave
some of this advice inside the binary, `llms.txt` gave a doc map, and `README.md`
gave a tour, but no single surface named the exact command that proves a given
outcome, and no gate held any of them to naming commands that still exist.

That last part is the reason this is a decision and not just a document. A
published contract that names a dead command is worse than no contract: it is
authoritative-looking instructions to run something that will fail, and prose
rots faster than the CLI it describes. Curie already has a class of gate for
exactly this shape, prose claims asserted against machine-readable ground truth,
in `tools/doclint`: citations resolved against the tree (#541) and seam counts
recomputed from the tree (#938). A command contract is the third instance of the
same class.

Two constraints shaped the outcome. First, `AGENTS.md` is already tracked at the
repository root, and a second root file named `agents.md` differs from it only in
case. Second, issue #1040, which adds an `info` verb reporting what a bundle
declares, was in flight on a separate branch and unmerged, while the draft
contract wanted it.

## Decision

**Publish the contract at `docs/agents.md`, not at the repository root.** On a
case-insensitive filesystem (macOS, Windows) git cannot materialize both
`AGENTS.md` and `agents.md`: checkout clobbers one with the other and leaves a
permanently dirty tree that no `git checkout --` can clean. Curie ships a
`darwin-arm64` release asset, so macOS contributors are real rather than
hypothetical. This is a correctness constraint, not a style preference, and it
must not be quietly undone later by a "move it to the root for discoverability"
change.

Three alternatives were weighed and rejected. **Renaming the contributor
`AGENTS.md`** to free the root slot: rejected, because `AGENTS.md` is the
cross-vendor convention filename agents auto-load from a repo root, so renaming
it silently removes the file those agents read, and the blast radius spans
`README.md`, `CONTRIBUTING.md`, `llms.txt`, and every per-area `CLAUDE.md`.
**Folding the contract into root `AGENTS.md` as a section**: rejected as the
primary home, because that file addresses agents working ON this repository
while the contract addresses agents USING Curie, most of whom installed a
released binary and have never cloned this repo; mixing the two audiences
degrades both, and the contract cannot travel to a user who has no checkout. A
pointer from `AGENTS.md` is added instead. **A non-colliding root filename such
as `AGENT-CONTRACT.md`**: rejected, because the only real benefit of root
placement is auto-discovery by agents that load convention filenames, and they
load `AGENTS.md`, not an invented name.

Reachability is bought with inbound links instead of with placement: `README.md`,
`llms.txt`, root `AGENTS.md`, `docs/README.md`, and `curie guide` all point at
`docs/agents.md`. "Published docs surface" resolves today to the
GitHub-rendered repository tree, `llms.txt`, and the released binary via
`curie guide`. Curie has no documentation website, so root placement would copy
a surface that does not exist here.

**The gate lives inside the doc-lint engine, as a fourth phase, not as its own
`curie dev` verb.** A standalone verb would need a new script, a new CI step, a
new `DevAction` variant with its dispatch arm and parse test, and regeneration
of both `cli/command-manifest.json` and the UI's generated mirror. Those last two
are the decisive cost: the gate's whole job is to resolve the contract against
`cli/command-manifest.json`, so adding the gate's own verb would mutate the very
artifact the gate reads, inside the same commit. Folding the check into
`tools/doclint` touches neither generated artifact and needs no CI change,
because `scripts/check-docs.sh` is already wired in CI. What is given up is
independent visibility, bought back by findings that name `docs/agents.md` and
the offending command explicitly, and by a phase echo and an amended `OK:` line
in `scripts/check-docs.sh`.

**`cli/command-manifest.json` is trustworthy ground truth** because
`cli/tests/command_surface.rs` already asserts the committed manifest matches the
live clap grammar and fails telling you to regenerate it. The manifest therefore
cannot silently drift from the real CLI, which is what makes resolving prose
against it meaningful rather than circular.

**Placeholders are banned in the contract.** Any token beginning with `<` is a
finding at any position. The tier set is closed at three, so writing skill,
local, and cluster out in full costs three lines, and enumerating all three
doubles as the ADR-0041 tier-parity proof that each verb is answered at each
tier. An expansion table would be a second source of truth that can itself drift
out of sync with the tier set, which is the failure class this gate exists to
prevent.

**The `info` bullet the issue drafted is deferred, not stubbed.** That verb does
not exist on `main`; #1040 implements it on a separate branch, unmerged.
Stubbing, aliasing, or naming it anyway would have put a command in the contract
that an agent cannot run, which is precisely the harm being gated against. The
post-scaffold outcome is covered instead by `curie skill check --json`, which is
behavioral: it boots the runner offline with no credential and reports whether
the bundle's declared MCP servers actually loaded. It does not cover the "lists
the expected skills" half of the draft, and the contract does not claim it does.
A follow-up adds that bullet once #1040 merges.

### Known boundaries of the gate

Recorded so nobody mistakes a green gate for more than it is.

- **The gate proves a command EXISTS. It does not prove the `--json` fields the
  contract asserts about (`verdict`, `declared`, `matches`, `connected`, `url`,
  `session`, `failed`, `plumbing_ok`) still exist or still mean what the
  contract says.** `curie skill eval` could keep its name while `plumbing_ok` is
  renamed, and the gate would stay green over a contract that is now wrong.
  Partially mitigated by the result schemas being committed and versioned under
  ADR-0074 and pinned by `cli/tests/schema_inventory.rs`. Resolving asserted
  field names against those schemas is the natural next increment; it is a second
  gate with its own parser and was deliberately not built here.
- **A command written in bare prose, outside backticks or a fenced block, is not
  scanned.** That boundary is what keeps the gate off ordinary English about the
  `curie` binary. It is bought back two ways: the contract states its own
  backtick house rule in its own body, and a vacuity guard fails the gate if the
  contract yields zero resolved commands at all.
- **Non-`curie` commands are unverified.** If a future edit adds a `docker` or
  `kubectl` invocation, the resolver skips it.

## Consequences

The contract is now the single place that says what proof means, and CI fails if
it names a command the CLI no longer has, proven by mutating the manifest rather
than by inspection. Four markdown surfaces plus the in-binary primer point at one
document instead of each carrying its own partial version, so they can no longer
drift into contradicting each other on command names: the primer is pinned
against the manifest by `cli/tests/guide.rs` and the contract is pinned against
the same manifest by the doc-lint gate.

The cost is that an agent browsing the repository root does not see the contract
without following a link, and that editing the contract now requires the author
to keep every command inside a code construct. Both are accepted, and both are
stated in the contract itself.
