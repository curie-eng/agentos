#!/usr/bin/env bash
# Refresh the schema compatibility baseline (ADR-0101).
#
# Copies cli/schema/*.schema.json over cli/schema/baseline/. Run it ONLY after a
# schema change has been versioned: adding an optional property bumps the minor
# (v1 -> v1.1); anything a conforming consumer previously accepted and no longer
# does bumps the major (v1 -> v2).
#
# Refreshing without bumping is exactly the defect the gate catches, so this
# script refuses when the gate would go from red to green purely because the
# baseline moved.
#
# It also refuses when the gate is currently GREEN and nothing needs refreshing.
# Running it speculatively is how the baseline stops being a record of the last
# published revision and becomes a copy of whatever is checked out, which costs
# the gate its reference point silently. (Learned by doing exactly that.)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
schema_dir="$root/cli/schema"
baseline_dir="$schema_dir/baseline"
check=false

if (( $# > 1 )); then
  echo "usage: $(basename "$0") [--check]" >&2
  exit 2
fi

case "${1:-}" in
  "")
    ;;
  --check)
    check=true
    ;;
  *)
    echo "usage: $(basename "$0") [--check]" >&2
    exit 2
    ;;
esac

shopt -s nullglob
live_schemas=("$schema_dir"/*.schema.json)

mismatched=()
for f in "${live_schemas[@]}"; do
  name="$(basename "$f")"
  base="$baseline_dir/$name"
  [[ -f "$base" ]] || continue
  cur_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["$id"])' "$f")"
  base_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["$id"])' "$base")"
  if [[ "$cur_id" == "$base_id" ]] && ! diff -q "$f" "$base" >/dev/null; then
    mismatched+=("$name ($cur_id)")
  fi
done

changed=()
for f in "${live_schemas[@]}"; do
  name="$(basename "$f")"
  base="$baseline_dir/$name"
  if [[ ! -f "$base" ]] || ! diff -q "$f" "$base" >/dev/null; then
    changed+=("$name")
  fi
done

baseline_only=()
for base in "$baseline_dir"/*.schema.json; do
  name="$(basename "$base")"
  [[ -f "$schema_dir/$name" ]] || baseline_only+=("$name")
done

if "$check"; then
  if (( ${#changed[@]} == 0 && ${#baseline_only[@]} == 0 )); then
    echo "baseline matches cli/schema/."
    exit 0
  fi

  echo "schema baseline is stale:" >&2
  if (( ${#changed[@]} > 0 )); then
    echo "  live schemas differing from their baseline:" >&2
    printf '    %s\n' "${changed[@]}" >&2
  fi
  if (( ${#baseline_only[@]} > 0 )); then
    echo "  baseline-only schemas:" >&2
    printf '    %s\n' "${baseline_only[@]}" >&2
  fi
  echo "run curie dev schema-baseline to refresh it." >&2
  exit 1
fi

if (( ${#changed[@]} == 0 && ${#baseline_only[@]} == 0 )); then
  echo "baseline already matches cli/schema/; nothing to refresh." >&2
  exit 0
fi

if (( ${#mismatched[@]} > 0 )); then
  echo "refusing to refresh: these schemas changed while keeping their \$id (ADR-0101)." >&2
  printf '  %s\n' "${mismatched[@]}" >&2
  echo >&2
  echo "Bump the version first -- minor for an added optional property, major for" >&2
  echo "anything a conforming consumer previously accepted -- then re-run." >&2
  echo "Refreshing here instead would make the gate green by moving the goalposts." >&2
  exit 1
fi

for name in "${changed[@]}"; do
  cp "$schema_dir/$name" "$baseline_dir/$name"
done
for name in "${baseline_only[@]}"; do
  rm -- "$baseline_dir/$name"
done

if (( ${#changed[@]} > 0 )); then
  echo "schema baseline refreshed for ${#changed[@]} schema(s):"
  printf '  %s\n' "${changed[@]}"
fi
if (( ${#baseline_only[@]} > 0 )); then
  echo "removed ${#baseline_only[@]} baseline-only schema(s):"
  printf '  %s\n' "${baseline_only[@]}"
fi
