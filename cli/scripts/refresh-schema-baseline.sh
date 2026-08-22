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
# The refusal reads SHAPE -- the properties-and-required reading
# `cli/tests/schema_inventory.rs` uses -- not a byte diff. ADR-0101 versions on a
# shape change, and that gate already skips `description`, `title` and
# `$comment` as prose. A byte diff here refused a corrected description outright
# and demanded a bump that would tell every consumer to refetch a schema whose
# shape did not move, which is neither what the ADR says nor what
# `cli/schema/baseline/README.md` says this script does.
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

# One schema's `$id` on the first line, then the JSON Pointer of every
# `properties` key and every `required` entry anywhere in the document,
# `$defs` included, sorted so the output is a stable fingerprint of the shape.
# Mirrors `shape`/`walk` in cli/tests/schema_inventory.rs, prose skip included;
# the two must agree, since that gate is what fails CI and this is what decides
# whether the baseline may be refreshed for it.
schema_shape() {
  python3 - "$1" <<'SHAPE'
import json, sys

# Prose, not shape: skipping these keeps a reworded description from reading as
# a compatibility event. Same list as the Rust gate.
PROSE_KEYS = ("description", "title", "$comment")


def walk(node, path, props, required):
    if isinstance(node, dict):
        declared = node.get("properties")
        if isinstance(declared, dict):
            props.update(f"{path}/properties/{key}" for key in declared)
        names = node.get("required")
        if isinstance(names, list):
            required.update(
                f"{path}/required/{name}" for name in names if isinstance(name, str)
            )
        for key, value in node.items():
            if key in PROSE_KEYS:
                continue
            walk(value, f"{path}/{key}", props, required)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, f"{path}/{index}", props, required)


with open(sys.argv[1], encoding="utf-8") as handle:
    schema = json.load(handle)
props, required = set(), set()
walk(schema, "", props, required)
print(schema.get("$id", "") if isinstance(schema, dict) else "")
for entry in sorted(props | required):
    print(entry)
SHAPE
}

mismatched=()
for f in "${live_schemas[@]}"; do
  name="$(basename "$f")"
  base="$baseline_dir/$name"
  [[ -f "$base" ]] || continue
  cur_shape="$(schema_shape "$f")"
  base_shape="$(schema_shape "$base")"
  cur_id="$(head -n1 <<<"$cur_shape")"
  base_id="$(head -n1 <<<"$base_shape")"
  if [[ "$cur_id" == "$base_id" && "$cur_shape" != "$base_shape" ]]; then
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
  echo "refusing to refresh: these schemas changed SHAPE while keeping their \$id (ADR-0101)." >&2
  printf '  %s\n' "${mismatched[@]}" >&2
  echo >&2
  echo "Bump the version first -- minor for an added optional property, major for" >&2
  echo "anything a conforming consumer previously accepted -- then re-run." >&2
  echo "Refreshing here instead would make the gate green by moving the goalposts." >&2
  exit 1
fi

# `${a[@]+"${a[@]}"}` rather than a bare `"${a[@]}"`: expanding an EMPTY array
# under `set -u` is an "unbound variable" error until bash 4.4, and macOS ships
# 3.2. Only ONE of these two lists has to be non-empty to get here (the both-empty
# case exited above), so the ordinary refresh -- a schema edited, none deleted --
# left baseline_only empty, copied the changed files, and only THEN aborted on the
# second loop, leaving a half-applied baseline behind a bogus error.
for name in ${changed[@]+"${changed[@]}"}; do
  cp "$schema_dir/$name" "$baseline_dir/$name"
done
for name in ${baseline_only[@]+"${baseline_only[@]}"}; do
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
