#!/usr/bin/env bash
# Prose style gate: em-dashes/en-dashes in prose, emoji in code and docs.
#
# AGENTS.md's commit conventions list four rules. Three of them got enforcement
# (the AI-attribution gate, the wire-lock gate, the ADR-number check). The
# fourth -- "No dashes/emdashes in prose content; no emojis in code or docs" --
# has been prose only, and prose only holds until someone is in a hurry.
#
# WHY THIS IS DIFF-SCOPED, NOT TREE-SCOPED. 103 tracked .md files already
# contain an em-dash, AGENTS.md itself among them. A tree-wide gate would fail
# on its first run and be deleted the same day. So this checks only the lines a
# PR ADDS, exactly like scripts/check-commit-messages.sh scopes to base..head:
# pre-gate content is not retroactively enforced. Clean up existing files when
# you are already editing them, not because a gate shouted.
#
# WHAT IT FLAGS, AND WHAT IT DELIBERATELY DOES NOT.
#   A. U+2014 EM DASH and U+2013 EN DASH, in added .md lines outside fenced
#      code blocks. Prose only. The rule is about typography, not punctuation.
#   B. Emoji, in added lines of any checked file, code included.
#
# It does NOT flag the ASCII hyphen. That is the single most important decision
# in this file. `--profile`, `check-docs.sh`, `| --- |`, `task/some-branch`,
# and every kebab-case identifier in the repo are hyphens; a gate that flagged
# them would fire thousands of times on honest lines and be switched off within
# a week. The rule's own words are "dashes/emdashes in prose", and the ASCII
# hyphen in a flag or a filename is neither.
#
# It also skips fenced code blocks inside markdown. Sample CLI output, captured
# logs, and quoted model responses land in fences and routinely carry em-dashes
# that are not ours to rewrite.
#
# ESCAPE HATCH. Put `prose:ignore-line` on the offending line or the line
# before it, mirroring the `<!-- doclint:ignore-line -->` convention the docs
# gate already uses. In markdown that is `<!-- prose:ignore-line -->`.
#
# USAGE
#   scripts/check-prose-style.sh <base>..<head>   # check a PR's added lines
#   scripts/check-prose-style.sh --self-test      # negative control
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

matcher() {
  python3 - "$@" <<'PY'
import re
import subprocess
import sys

# Emoji ranges. Pictographs, regional indicators, misc symbols, dingbats, and
# the variation selector that turns a text glyph into an emoji one. Deliberately
# excludes the arrows and box-drawing blocks: this repo draws ASCII diagrams and
# uses -> in prose constantly.
#
# Written as escapes, never as literal characters. This file has to name the
# things it forbids, and a literal here would make the gate fail on its own
# source, which is a fine way to discover the escape hatch and a bad way to
# leave the repo.
EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0F"
    "]"
)
EM_EN_DASH = re.compile("[\u2014\u2013]")
IGNORE = "prose:ignore-line"

# Generated artifacts and captured third-party text. A model response stored as
# a fixture is evidence, not our prose, and rewriting it would corrupt the test.
SKIP_PREFIXES = (
    "docs/interfaces.md",
    "docs/interfaces/",
    "docs/adr/README.md",
)
SKIP_SUBSTRINGS = ("/fixtures/", "/testdata/", "/__snapshots__/")
SKIP_SUFFIXES = (".lock", ".svg", ".min.js", ".po", ".pot")

PROSE_SUFFIXES = (".md",)


def added_lines(rng):
    """Map path -> set of added line numbers, from a zero-context diff."""
    out = subprocess.run(
        ["git", "diff", "--unified=0", "--diff-filter=d", rng],
        capture_output=True, text=True, check=True,
    ).stdout
    result, path = {}, None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = 1 if m.group(2) is None else int(m.group(2))
                result.setdefault(path, set()).update(
                    range(start, start + count)
                )
    return result


def skipped(path):
    return (
        path.startswith(SKIP_PREFIXES)
        or any(s in path for s in SKIP_SUBSTRINGS)
        or path.endswith(SKIP_SUFFIXES)
    )


def scan(path, wanted, lines):
    """Yield (lineno, rule, text) for each violation on a wanted line."""
    is_prose = path.endswith(PROSE_SUFFIXES)
    in_fence = False
    findings = []
    for idx, text in enumerate(lines, start=1):
        stripped = text.lstrip()
        # Track fences on every line, not just wanted ones: fence state is
        # cumulative and a diff only gives us a subset of the file.
        if is_prose and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = not in_fence
            continue
        if idx not in wanted:
            continue
        if IGNORE in text:
            continue
        if idx > 1 and IGNORE in lines[idx - 2]:
            continue
        if EMOJI.search(text):
            findings.append((idx, "emoji", text))
        elif is_prose and not in_fence and EM_EN_DASH.search(text):
            findings.append((idx, "em/en dash", text))
    return findings


def check(rng):
    violations = []
    for path, wanted in sorted(added_lines(rng).items()):
        if skipped(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue  # binary or deleted between diff and read
        for lineno, rule, text in scan(path, wanted, lines):
            violations.append((path, lineno, rule, text.strip()))
    return violations


def self_test():
    """Negative control. A gate that cannot fail is not a gate."""
    must_flag = [
        ("a.md", "the plan \u2014 as written \u2014 is fine", "em/en dash"),
        ("a.md", "range 1\u20132 inclusive", "em/en dash"),
        ("a.md", "shipped \U0001F680 today", "emoji"),
        ("src/lib.rs", "// done \u2705", "emoji"),
    ]
    must_pass = [
        ("a.md", "run with --profile full"),
        ("a.md", "see scripts/check-docs.sh for the mirror"),
        ("a.md", "| --- | --- |"),
        ("a.md", "branch task/some-fix-here"),
        ("a.md", "Pydantic -> JSON Schema -> TS"),
        ("a.md", "the plan \u2014 fine <!-- prose:ignore-line -->"),
        ("src/lib.rs", "let x = a - b; // plain hyphen"),
        ("src/lib.rs", "// an em dash \u2014 in code is not prose"),
    ]
    failures = []
    for path, text, want in must_flag:
        got = scan(path, {1}, [text])
        if not got or got[0][1] != want:
            failures.append(f"MISSED  {path}: {text!r} (wanted {want}, got {got})")
    for path, text in must_pass:
        got = scan(path, {1}, [text])
        if got:
            failures.append(f"FALSE+  {path}: {text!r} -> {got}")
    # Fence state must suppress a dash inside a code block but not after it.
    fenced = ["```", "output \u2014 here", "```", "prose \u2014 here"]
    got = scan("a.md", {2, 4}, fenced)
    if [g[0] for g in got] != [4]:
        failures.append(f"FENCE   expected only line 4 flagged, got {got}")
    if failures:
        print("SELF-TEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: self-test passed ({len(must_flag)} caught, {len(must_pass)} allowed, fences respected).")


if sys.argv[1] == "--self-test":
    self_test()
else:
    found = check(sys.argv[1])
    if found:
        print("ERROR: prose style violations on lines this change adds:", file=sys.stderr)
        for path, lineno, rule, text in found:
            print(f"  {path}:{lineno}  [{rule}]  {text}", file=sys.stderr)
        print("", file=sys.stderr)
        print("AGENTS.md: no dashes/emdashes in prose content; no emojis in code", file=sys.stderr)
        print("or docs. Replace an em-dash with a comma, a colon, or a full stop.", file=sys.stderr)
        print("If a line is genuinely quoted or generated, mark it with", file=sys.stderr)
        print("`prose:ignore-line` on that line or the line above.", file=sys.stderr)
        sys.exit(1)
    print("OK: no em-dashes, en-dashes, or emoji on added lines.")
PY
}

if [ "${1:---self-test}" = "--self-test" ]; then
  echo "== prose style gate: self-test (negative control) =="
  matcher --self-test
else
  echo "== prose style gate: checking lines added by $1 =="
  matcher "$1"
fi
