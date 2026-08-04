#!/usr/bin/env bash
#
# Render-assertion test for issue #756 (ADR-0059 decision 2). Every writable
# `emptyDir` volume in the sandbox pod -- the shared `bundles` volume, the
# init only `aws-config` volume, and one volume per `hardening.writablePaths`
# entry -- must carry an explicit `sizeLimit` so a pod that overruns is
# evicted on its own account instead of exhausting node disk and taking every
# co-scheduled pod down with it (node-wide `DiskPressure`). This test pins
# that every emptyDir volume rendered in the SandboxTemplate carries a
# non-empty sizeLimit, on both the default `writablePaths` list and a
# lengthened one, so the mechanism is proven generic rather than hardcoded to
# `/tmp` and `/home/runner`.
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
EXTRA_PATH="$TMP/extra-path.yaml"

echo "=== Rendering SandboxTemplate (defaults: bundles, aws-config, /tmp, /home/runner) ==="
helm template rel "$CHART" --show-only "$TPL" > "$DEFAULT"

echo "=== Rendering SandboxTemplate (a third writablePaths entry appended) ==="
helm template rel "$CHART" --show-only "$TPL" \
  --set agentSandbox.runner.hardening.writablePaths[0]=/tmp \
  --set agentSandbox.runner.hardening.writablePaths[1]=/home/runner \
  --set agentSandbox.runner.hardening.writablePaths[2]=/var/scratch \
  --set agentSandbox.runner.hardening.writablePathSizeLimit=256Mi > "$EXTRA_PATH"

ASSERT_PY="$TMP/assert.py"
cat > "$ASSERT_PY" <<'PY'
import sys, yaml


def sandbox_template(path):
    for doc in yaml.safe_load_all(open(path)):
        if doc and doc.get("kind") == "SandboxTemplate":
            return doc
    raise SystemExit(f"no SandboxTemplate rendered in {path}")


def emptydir_volumes(path):
    tmpl = sandbox_template(path)
    spec = tmpl["spec"]["podTemplate"]["spec"]
    return [v for v in (spec.get("volumes") or []) if "emptyDir" in v]


def check(path, expected_names, expected_size_limit=None):
    volumes = emptydir_volumes(path)
    names = {v["name"] for v in volumes}
    missing = set(expected_names) - names
    if missing:
        raise SystemExit(
            f"{path}: expected emptyDir volumes {sorted(missing)} not rendered "
            f"(got {sorted(names)})"
        )
    unset = []
    for v in volumes:
        limit = (v.get("emptyDir") or {}).get("sizeLimit")
        if not limit:
            unset.append(v["name"])
    if unset:
        raise SystemExit(
            f"{path}: emptyDir volume(s) {sorted(unset)} have no sizeLimit set "
            "(ADR-0059 decision 2 requires one on every writable emptyDir)"
        )
    if expected_size_limit is not None:
        wrong = {
            v["name"]: v["emptyDir"]["sizeLimit"]
            for v in volumes
            if v["name"].startswith("writable-")
            and v["emptyDir"]["sizeLimit"] != expected_size_limit
        }
        if wrong:
            raise SystemExit(
                f"{path}: writablePaths volume(s) did not honor the overridden "
                f"writablePathSizeLimit={expected_size_limit!r}: {wrong}"
            )
    print(f"  ok: {sorted(names)} all carry a non-empty sizeLimit")


# Default render: bundles, aws-config, and one writable-N per default
# writablePaths entry (/tmp, /home/runner -> writable-0, writable-1).
check(sys.argv[1], {"bundles", "aws-config", "writable-0", "writable-1"})

# A third writablePaths entry must ALSO get a sizeLimit -- proves the
# mechanism is generic over the list, not hardcoded to two hand-picked paths
# -- and the overridden writablePathSizeLimit must apply to every one of them.
check(
    sys.argv[2],
    {"bundles", "aws-config", "writable-0", "writable-1", "writable-2"},
    expected_size_limit="256Mi",
)
PY

if ! out="$(python3 "$ASSERT_PY" "$DEFAULT" "$EXTRA_PATH" 2>&1)"; then
  fail "$out"
fi
echo "$out"

echo
echo "PASS: every emptyDir volume in the rendered sandbox pod (bundles, aws-config, and one per hardening.writablePaths entry, including a lengthened list) carries an explicit, operator-overridable sizeLimit."
