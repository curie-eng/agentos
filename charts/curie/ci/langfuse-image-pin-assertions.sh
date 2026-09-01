#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$CHART/../.." && pwd)"
PIN="3.225.5"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

check_consumers() {
  local chart="$1" compose="$2" label="$3"
  local rendered="$TMP/${label}-rendered.yaml"
  local compose_images="$TMP/${label}-compose-images.txt"

  helm template rel "$chart" >"$rendered"
  docker compose -f "$compose" --profile full config --images >"$compose_images"

  grep -Fq "image: \"langfuse/langfuse:${PIN}\"" "$rendered" \
    || fail "$label Helm web image is not pinned to ${PIN}"
  grep -Fq "image: \"langfuse/langfuse-worker:${PIN}\"" "$rendered" \
    || fail "$label Helm worker image is not pinned to ${PIN}"
  grep -Fxq "langfuse/langfuse:${PIN}" "$compose_images" \
    || fail "$label Compose web image is not pinned to ${PIN}"
  grep -Fxq "langfuse/langfuse-worker:${PIN}" "$compose_images" \
    || fail "$label Compose worker image is not pinned to ${PIN}"

  if grep -Eq 'langfuse/langfuse(-worker)?:3(["[:space:]]|$)' "$rendered" "$compose_images"; then
    fail "$label accepted a floating Langfuse major tag"
  fi
}

check_consumers "$CHART" "$ROOT/compose.dev.yaml" baseline

MUTANT_CHART="$TMP/chart"
MUTANT_COMPOSE="$TMP/compose.dev.yaml"
cp -a "$CHART" "$MUTANT_CHART"
cp "$ROOT/compose.dev.yaml" "$MUTANT_COMPOSE"
python3 - "$MUTANT_CHART/values.yaml" "$MUTANT_COMPOSE" "$PIN" <<'PY'
from pathlib import Path
import sys

values_path = Path(sys.argv[1])
compose_path = Path(sys.argv[2])
pin = sys.argv[3]

values = values_path.read_text()
needle = f'tag: "{pin}"'
if values.count(needle) != 1:
    raise SystemExit(f"FAIL: expected one chart pin to mutate, found {values.count(needle)}")
values_path.write_text(values.replace(needle, 'tag: "3"'))

compose = compose_path.read_text()
for image in ("langfuse/langfuse", "langfuse/langfuse-worker"):
    needle = f"{image}:{pin}"
    if compose.count(needle) != 1:
        raise SystemExit(
            f"FAIL: expected one Compose pin for {image} to mutate, found {compose.count(needle)}"
        )
    compose = compose.replace(needle, f"{image}:3")
compose_path.write_text(compose)
PY

if (check_consumers "$MUTANT_CHART" "$MUTANT_COMPOSE" mutation) >/dev/null 2>&1; then
  fail "floating Langfuse tag mutation was accepted"
fi

echo "Langfuse image pin assertions passed"
