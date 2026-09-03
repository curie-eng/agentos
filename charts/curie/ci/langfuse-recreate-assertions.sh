#!/usr/bin/env bash
#
# Render-assertion test for issue #2216 (Langfuse RollingUpdate races boot
# migrations). Both Langfuse Deployments run Prisma and ClickHouse migrations
# at container boot with no cross-process lock. With replicas: 1 and no
# strategy, Kubernetes defaults to RollingUpdate and maxSurge 25% rounds up to
# one extra pod, so an image change runs two migrators against the same
# database.
#
# This asserts that helm template renders spec.strategy.type Recreate on both
# langfuse-web and langfuse-worker. Recreate stops the old pod before the new
# one starts, so only one pod ever runs those boot migrations. A mutant that
# restores RollingUpdate on either Deployment is rejected, so a checker that
# only looked at one of the two siblings would not pass.
#
# Runnable locally (from anywhere) and from CI. Fails loudly, naming the
# Deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RENDER="$TMP/langfuse.yaml"
CHECKER="$TMP/check.py"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

echo "=== Rendering templates/langfuse.yaml ==="
helm template curie "$CHART" --show-only templates/langfuse.yaml >"$RENDER"

cat >"$CHECKER" <<'PY'
import pathlib
import sys

import yaml

path = sys.argv[1]
wanted = ("-langfuse-web", "-langfuse-worker")
found = {}

for document in yaml.safe_load_all(pathlib.Path(path).read_text()):
    if not document or document.get("kind") != "Deployment":
        continue
    name = document.get("metadata", {}).get("name", "")
    for suffix in wanted:
        if name.endswith(suffix):
            found[suffix] = document
            break

problems = []
for suffix in wanted:
    document = found.get(suffix)
    if document is None:
        problems.append(
            f"no Deployment ending in {suffix!r} rendered; cannot pin Recreate"
        )
        continue
    name = document.get("metadata", {}).get("name")
    spec = document.get("spec") or {}
    strategy_type = (spec.get("strategy") or {}).get("type")
    if strategy_type != "Recreate":
        problems.append(
            f"{name} spec.strategy.type must be Recreate so an image change "
            f"never runs two boot migrations at once; found {strategy_type!r}"
        )
if problems:
    raise SystemExit("\n".join(problems))

print(
    "langfuse-web and langfuse-worker Deployments both render "
    "spec.strategy.type Recreate"
)
PY

echo "=== Assertion: both Langfuse Deployments render Recreate ==="
python3 "$CHECKER" "$RENDER"

flip_strategy() {
  local suffix="$1"
  local dest="$2"
  python3 - "$RENDER" "$dest" "$suffix" <<'PY'
import pathlib
import sys

import yaml

source, dest, suffix = sys.argv[1], sys.argv[2], sys.argv[3]
documents = list(yaml.safe_load_all(pathlib.Path(source).read_text()))
mutated = False
for document in documents:
    if not document or document.get("kind") != "Deployment":
        continue
    name = document.get("metadata", {}).get("name", "")
    if name.endswith(suffix):
        document.setdefault("spec", {}).setdefault("strategy", {})["type"] = "RollingUpdate"
        mutated = True
if not mutated:
    raise SystemExit(f"mutant setup failed: no Deployment ending in {suffix!r} to flip")
with pathlib.Path(dest).open("w") as handle:
    yaml.safe_dump_all(documents, handle)
PY
}

reject_rolling_update() {
  local suffix="$1"
  local mutant="$TMP/langfuse-rolling${suffix}.yaml"
  echo "=== Negative: RollingUpdate on ${suffix#-} is rejected ==="
  flip_strategy "$suffix" "$mutant"
  local negative_output=""
  if negative_output="$(python3 "$CHECKER" "$mutant" 2>&1)"; then
    fail "RollingUpdate mutation on ${suffix#-} passed the Recreate contract"
  fi
  if [[ "$negative_output" != *"spec.strategy.type must be Recreate"* ]]; then
    fail "RollingUpdate mutation on ${suffix#-} failed unexpectedly: $negative_output"
  fi
  if [[ "$negative_output" != *"${suffix#-}"* ]]; then
    fail "RollingUpdate mutation did not name ${suffix#-}: $negative_output"
  fi
  echo "negative: flipping ${suffix#-} to RollingUpdate is rejected"
}

reject_rolling_update "-langfuse-web"
reject_rolling_update "-langfuse-worker"

echo "PASS: both Langfuse Deployments render Recreate and a RollingUpdate sibling is refused (issue #2216)"
