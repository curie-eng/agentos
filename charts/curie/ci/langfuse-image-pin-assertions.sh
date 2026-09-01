#!/usr/bin/env bash
#
# Issue #2190: keep the shipped Langfuse runtime on one reviewed version.
#
# A floating `:3` moved from 3.225.5 to 3.225.6 during one CI run. The newly
# pulled image left ClickHouse migration 39 dirty in the real delayed-Postgres
# chart consumer and made the Compose observability query return 500. This gate
# renders both user-facing consumers, requires one exact version, and proves a
# floating-tag mutation is rejected.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CHART/../.." && pwd)"
EXPECTED_VERSION="3.225.5"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RENDER="$TMP/chart.yaml"
COMPOSE_JSON="$TMP/compose.json"
CHECKER="$TMP/check.py"

helm template curie "$CHART" >"$RENDER"
docker compose --profile full -f "$REPO_ROOT/compose.dev.yaml" config --format json >"$COMPOSE_JSON"

cat >"$CHECKER" <<'PY'
import json
import pathlib
import sys

import yaml

chart_path, compose_path, expected_version = sys.argv[1:]
expected = {
    "langfuse-web": f"langfuse/langfuse:{expected_version}",
    "langfuse-worker": f"langfuse/langfuse-worker:{expected_version}",
}

documents = [
    document
    for document in yaml.safe_load_all(pathlib.Path(chart_path).read_text())
    if document
]
chart_images = {}
for document in documents:
    if document.get("kind") != "Deployment":
        continue
    containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        name = container.get("name")
        if name in expected:
            chart_images[name] = container.get("image")

compose = json.loads(pathlib.Path(compose_path).read_text())
compose_images = {
    name: compose.get("services", {}).get(name, {}).get("image")
    for name in expected
}

problems = []
for name, wanted in expected.items():
    for surface, actual in (("chart", chart_images.get(name)), ("compose", compose_images.get(name))):
        if actual != wanted:
            problems.append(
                f"{surface} {name} must use reviewed image {wanted}; found {actual!r} "
                "(floating Langfuse tags are forbidden)"
            )
if problems:
    raise SystemExit("\n".join(problems))

print(f"chart and Compose pin Langfuse web/worker to {expected_version}")
PY

python3 "$CHECKER" "$RENDER" "$COMPOSE_JSON" "$EXPECTED_VERSION"

MUTANT_RENDER="$TMP/chart-floating.yaml"
MUTANT_COMPOSE="$TMP/compose-floating.json"
python3 - "$RENDER" "$COMPOSE_JSON" "$MUTANT_RENDER" "$MUTANT_COMPOSE" "$EXPECTED_VERSION" <<'PY'
import pathlib
import sys

render, compose, mutant_render, mutant_compose, version = sys.argv[1:]
pathlib.Path(mutant_render).write_text(pathlib.Path(render).read_text().replace(f":{version}", ":3"))
pathlib.Path(mutant_compose).write_text(pathlib.Path(compose).read_text().replace(f":{version}", ":3"))
PY

negative_output=""
if negative_output="$(python3 "$CHECKER" "$MUTANT_RENDER" "$MUTANT_COMPOSE" "$EXPECTED_VERSION" 2>&1)"; then
  echo "FAIL: floating-tag mutation passed the Langfuse image contract" >&2
  exit 1
fi
if [[ "$negative_output" != *"floating Langfuse tags are forbidden"* ]]; then
  echo "FAIL: floating-tag mutation failed unexpectedly: $negative_output" >&2
  exit 1
fi

echo "negative: replacing the reviewed version with :3 is rejected"
echo "PASS: chart and Compose share one reviewed Langfuse version and reject floating tags"
