---
name: update-architecture-atlas
description: Create or refresh a versioned Curie architecture-atlas JSON snapshot from a git diff, including current and vision flows, maturity-rated seams, ADRs, and documentation drift. Use when asked to update, regenerate, compare, or add a version to the interactive architecture atlas.
---

# Update the architecture atlas

Keep `docs/architecture-atlas/` an evidence-backed, versioned view of Curie's
current architecture and accepted vision. The JSON snapshots are the source of
truth; `index.html` is a generic renderer.

## Create a version

Resolve the exact target commit and the prior registered version. Create the
new file and manifest entry with:

```bash
python3 .claude/skills/update-architecture-atlas/scripts/new_snapshot.py \
  --version <version-id> \
  --commit <full-target-commit> \
  --branch <branch> \
  --date <YYYY-MM-DD>
```

Use `--from-version` when the default snapshot is not the correct baseline.
The helper copies data; it does not claim that the copied facts are current.

Never overwrite a registered snapshot. A historical version is immutable. If
its renderer needs a compatibility fix, change the generic HTML or bump the
atlas schema with an explicit migration.

## Research the diff

Read `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, and `llms.txt`, then inspect:

```bash
git diff --name-status <prior-commit>..<target-commit>
git log --oneline --decorate <prior-commit>..<target-commit>
```

Group changed paths by ownership boundary. For each affected component or
cross-component seam, read its scoped `CLAUDE.md`, `INTERFACE.md`, relevant ADRs,
and the implementation at the target commit. Use generated
`docs/interfaces.md` as an index, not as sole proof when it conflicts with code.

Update only facts that the diff can affect. Preserve untouched descriptions,
positions, and identifiers so version comparison remains meaningful. Ground
external SDK or API claims in provider documentation or observed behavior.

## Model the architecture

- Keep implementations collapsed behind stable roles. Adding another channel,
  substrate, model endpoint, or MCP server should expand its implementation
  disclosure, not add another top-level architecture box.
- Show current service-key MCP auth separately from planned per-person OAuth.
  Service and delegated identity modes must be explicit and have no fallback.
- Use `current` for shipped routes and `planned` for accepted or draft vision.
  Do not draw planned behavior as current because an ADR exists.
- Set `bidirectional: true` for one request/response route. Opposing raw routes
  between the same boxes are rendered as one line with two arrowheads and one
  tooltip; keep both raw routes only when distinct flow steps need their own ids.
- Keep every conceptual node inside its declared zone. Move nodes before
  shrinking labels or boxes.
- Store detailed text in node, seam, flow, and ADR records. Map bubbles stay
  title-only; tooltips and the inspector carry the detail.

Classify substitutability, not health:

- `green`: multiple real implementations exercise the same boundary.
- `yellow`: one real implementation sits behind a credible clean interface.
- `red`: replacing it still requires coordinated knowledge or changes across
  owners.
- `planned`: the intended boundary is documented but has no real implementation.

Do not promote a seam based on types, tests, or an ADR alone. Name concrete
implementations and the evidence that they exercise the boundary.

## Decisions and drift

Add or update ADR records when a decision changes the visible current or vision
architecture. Link to a commit-pinned public URL and use placeholder identifiers
required by `AGENTS.md`.

Re-verify every `documentationDrift` entry against the target commit. Remove a
resolved drift. Add a drift only when current code and current documentation make
different claims, and include resolvable evidence ids. Creating, closing, or
commenting on GitHub issues is a separate external mutation and requires explicit
authorization.

## Validate and preview

Run:

```bash
python3 .claude/skills/update-architecture-atlas/scripts/validate_atlas.py
python3 -m http.server 8767 --directory docs/architecture-atlas
```

Open <http://localhost:8767/> and inspect Current, Vision, and Delta at desktop
width. Confirm the new version selector entry, rich route tooltips, collapsed
implementation disclosures, and that no box overlaps or escapes its zone. Stop
the temporary server when verification is complete.

Finish with the exact target commit, compared range, changed architecture facts,
remaining uncertainty, and the validator output. Do not run product E2E tiers for
a JSON-and-renderer-only update.
