#!/usr/bin/env bash
#
# Render-assertion test for issue #755 (ADR-0059 decision 1: no unbounded
# resource dimension on a sandbox container). Proves:
#
#   1. DEFAULT render: the runner container and both bundle init containers
#      (bundle-fetch, bundle-extract) each carry an `ephemeral-storage`
#      request AND limit alongside cpu/memory in `resources`.
#   2. OVERRIDE: an operator `--set` on `agentSandbox.runner.resources.limits`
#      changes the rendered ephemeral-storage limit, proving the ceiling is
#      operator-overridable per ADR-0059 decision 6 (the `RunnerHardening`
#      precedent), not hardcoded in the template.
#
# Before this issue, disk was the one resource dimension with no ceiling at
# all: no `resources` block anywhere in the chart set `ephemeral-storage`, so
# every pod's request was zero and the kubelet had nothing to schedule or
# measure an overrunning pod against -- the only backstop was node-level
# DiskPressure eviction, which degrades every co-scheduled pod before it fires.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TPL=templates/agent-sandbox.yaml

fail() { echo "FAIL: $*" >&2; exit 1; }

DEFAULT="$TMP/default.yaml"
OVERRIDE="$TMP/override.yaml"

echo "=== Rendering SandboxTemplate (defaults) ==="
helm template rel "$CHART" --show-only "$TPL" > "$DEFAULT"

echo "=== Rendering SandboxTemplate (operator override of the runner limit) ==="
helm template rel "$CHART" --show-only "$TPL" \
  --set agentSandbox.runner.resources.limits.ephemeral-storage=9Gi > "$OVERRIDE"

ASSERT_PY="$TMP/assert.py"
cat > "$ASSERT_PY" <<'PY'
import sys, yaml


def sandbox_template(path):
    for doc in yaml.safe_load_all(open(path)):
        if doc and doc.get("kind") == "SandboxTemplate":
            return doc
    raise SystemExit(f"no SandboxTemplate rendered in {path}")


def containers(path):
    tmpl = sandbox_template(path)
    spec = tmpl["spec"]["podTemplate"]["spec"]
    return (spec.get("initContainers") or []) + (spec.get("containers") or [])


def check_present(path, expected_names):
    found = {c["name"]: c for c in containers(path)}
    missing = set(expected_names) - set(found)
    if missing:
        raise SystemExit(f"{path}: expected containers {sorted(missing)} not rendered (got {sorted(found)})")
    for name in expected_names:
        res = found[name].get("resources") or {}
        requests = res.get("requests") or {}
        limits = res.get("limits") or {}
        if "ephemeral-storage" not in requests:
            raise SystemExit(
                f"{path}: container {name!r} has no ephemeral-storage REQUEST "
                f"(ADR-0059 decision 1); got requests={requests}"
            )
        if "ephemeral-storage" not in limits:
            raise SystemExit(
                f"{path}: container {name!r} has no ephemeral-storage LIMIT "
                f"(ADR-0059 decision 1); got limits={limits}"
            )
    print(f"  ok: {sorted(expected_names)} all carry an ephemeral-storage request and limit")


def check_override(path, expected_limit):
    found = {c["name"]: c for c in containers(path)}
    for name in ("runner", "bundle-fetch", "bundle-extract"):
        got = (found[name].get("resources") or {}).get("limits", {}).get("ephemeral-storage")
        if got != expected_limit:
            raise SystemExit(
                f"{path}: container {name!r} ephemeral-storage limit override not honored; "
                f"expected {expected_limit!r}, got {got!r}"
            )
    print(f"  ok: runner + both init containers honor the overridden ephemeral-storage limit ({expected_limit})")


check_present(sys.argv[1], {"bundle-fetch", "bundle-extract", "runner"})
check_override(sys.argv[2], "9Gi")
PY

if ! out="$(python3 "$ASSERT_PY" "$DEFAULT" "$OVERRIDE" 2>&1)"; then
  fail "$out"
fi
echo "$out"

echo
echo "PASS: the runner and bundle-fetch/bundle-extract init containers render an ephemeral-storage request and limit, and the ceiling is operator-overridable (ADR-0059 decisions 1 and 6)."
