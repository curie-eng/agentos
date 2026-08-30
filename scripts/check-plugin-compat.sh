#!/usr/bin/env bash
# Assert every bundle under examples/ still validates as a Claude Code plugin.
# The bundle format is the Claude Code plugin shape verbatim (the distribution
# wedge), so outbound compatibility is a contract, not a claim: this is the
# local mirror of the CI plugin-compat gate. Plain `claude plugin validate`, no
# --strict: our six authoring extensions (systemPrompt, starterPrompts,
# secrets, triggers, approvalPolicy, toolPolicy) are unknown-to-Claude-Code by
# design, and --strict would promote those unknown-field warnings to errors.
# Warnings are expected and allowed; a non-zero exit is the failure signal.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: the 'claude' CLI is not on PATH, so outbound compatibility cannot be checked." >&2
  echo "Install it (npm i -g @anthropic-ai/claude-code) and re-run." >&2
  exit 1
fi

echo "== discovering bundles under examples/ =="
bundles=()
while IFS= read -r manifest; do
  bundles+=("$(dirname "$(dirname "$manifest")")")
done < <(find examples -mindepth 3 -maxdepth 3 -path '*/.claude-plugin/plugin.json' | sort)

if [ "${#bundles[@]}" -eq 0 ]; then
  echo "ERROR: no bundles found under examples/ (looked for */.claude-plugin/plugin.json)." >&2
  echo "An empty match would pass this gate vacuously, which defeats its purpose." >&2
  exit 1
fi
echo "found ${#bundles[@]} bundle(s): ${bundles[*]}"

echo "== validating each bundle against Claude Code =="
failed=()
for bundle in "${bundles[@]}"; do
  echo "-- $bundle --"
  if ! claude plugin validate "$bundle"; then
    failed+=("$bundle")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "ERROR: Claude Code rejected ${#failed[@]} bundle(s): ${failed[*]}" >&2
  echo "The bundle format is the Claude Code plugin shape verbatim. Either the bundle" >&2
  echo "drifted, or Claude Code changed the format under us. Fix the bundle or update" >&2
  echo "docs/interfaces/bundle-format/INTERFACE.md to record the new contract." >&2
  exit 1
fi

echo "OK: all ${#bundles[@]} bundle(s) validate as Claude Code plugins."
