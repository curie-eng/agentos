#!/usr/bin/env bash
#
# Render-assertion test for issue #493 (Rail 3 container hardening). The
# container-level securityContext lockdown is applied to EVERY container in the
# untrusted sandbox pod -- the runner, both bundle init containers, and
# workspace-init -- and was previously copy-pasted four times. It now renders
# from one `curie.sandboxHardening.securityContext` helper; this test pins that
# the rendered lockdown is present and identical on every container, so a
# helper edit that weakens (or a container that skips) the lockdown fails CI.
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

echo "=== Rendering SandboxTemplate (defaults; hardening on) ==="
helm template rel "$CHART" --show-only "$TPL" > "$DEFAULT"

ASSERT_PY="$TMP/assert.py"
cat > "$ASSERT_PY" <<'PY'
import sys, yaml

# The hardened container securityContext every sandbox container must carry.
EXPECTED = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "runAsNonRoot": True,
    "capabilities": {"drop": ["ALL"]},
}


def sandbox_template(path):
    for doc in yaml.safe_load_all(open(path)):
        if doc and doc.get("kind") == "SandboxTemplate":
            return doc
    raise SystemExit(f"no SandboxTemplate rendered in {path}")


def check(path, expected_names):
    tmpl = sandbox_template(path)
    spec = tmpl["spec"]["podTemplate"]["spec"]
    containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
    names = {c["name"] for c in containers}
    missing = set(expected_names) - names
    if missing:
        raise SystemExit(f"{path}: expected containers {sorted(missing)} not rendered (got {sorted(names)})")
    for c in containers:
        sc = c.get("securityContext")
        if sc != EXPECTED:
            raise SystemExit(
                f"{path}: container {c['name']!r} securityContext is not the hardened "
                f"lockdown.\n  expected: {EXPECTED}\n  got:      {sc}"
            )
    print(f"  ok: {sorted(names)} all carry the identical hardened securityContext")


check(sys.argv[1], {"bundle-fetch", "bundle-extract", "runner", "workspace-init"})
PY

if ! out="$(python3 "$ASSERT_PY" "$DEFAULT" 2>&1)"; then
  fail "$out"
fi
echo "$out"

echo
echo "PASS: every sandbox container (runner, bundle-fetch, bundle-extract, and workspace-init) renders the identical Rail 3 hardened securityContext from the shared helper."
