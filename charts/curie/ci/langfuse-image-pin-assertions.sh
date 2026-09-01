#!/usr/bin/env bash
#
# Issue #2190: keep the shipped Langfuse runtime on one reviewed version.
#
# On 2026-09-01T14:50:59Z upstream shipped v3.225.6 by rebuilding the
# floating `:3` tag and the minor tag `3.225` in place onto its new digest,
# rather than publishing a `3.225.6` tag — so a minor-tag pin would have
# moved too (the exact patch tag `3.225.5` was unaffected). In CI, the
# langfuse-web container reported healthy but never served
# /api/public/health, timing out the bring-up gate. This gate renders both
# user-facing consumers, requires one exact version, and proves a
# floating-tag mutation is rejected.
#
# The chart runs Langfuse images in initContainers as well as application
# containers (the wait-for-clickhouse helper reuses the same image), and both
# are pulled and EXECUTED before the application container starts. Both
# initContainers are named `wait-for-clickhouse`, in both Deployments, so a
# name-keyed expectation cannot address them. The chart-side rule is therefore
# positional-free: the enclosing Deployment decides which reviewed image
# applies, and EVERY container and initContainer in it whose image is a
# `langfuse/` image must equal that image exactly.
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
LANGFUSE_IMAGE_PREFIX = "langfuse/"
SECTIONS = ("initContainers", "containers")

documents = [
    document
    for document in yaml.safe_load_all(pathlib.Path(chart_path).read_text())
    if document
]

problems = []
# name -> {"deployment": str, "initContainers": int, "containers": int}. Counts
# are the anti-vacuity ledger: a scan that finds nothing must not report PASS.
found = {}

for document in documents:
    if document.get("kind") != "Deployment":
        continue
    deployment = document.get("metadata", {}).get("name")
    pod = document.get("spec", {}).get("template", {}).get("spec", {})
    sections = {section: pod.get(section) or [] for section in SECTIONS}
    langfuse_refs = [
        (section, container)
        for section in SECTIONS
        for container in sections[section]
        if str(container.get("image", "")).startswith(LANGFUSE_IMAGE_PREFIX)
    ]
    # The application container's name is what identifies which reviewed image
    # governs this Deployment; the initContainers inherit that decision.
    owners = [
        container.get("name")
        for container in sections["containers"]
        if container.get("name") in expected
    ]
    if len(owners) != 1:
        if langfuse_refs or owners:
            problems.append(
                f"chart Deployment {deployment} carries {len(langfuse_refs)} Langfuse image "
                f"reference(s) but {len(owners)} known Langfuse application container(s) "
                f"{owners!r}, so no reviewed image can be attributed to it "
                "(floating Langfuse tags are forbidden)"
            )
        continue

    owner = owners[0]
    wanted = expected[owner]
    if owner in found:
        problems.append(
            f"chart application container {owner} rendered twice "
            f"({found[owner]['deployment']} and {deployment})"
        )
        continue
    seen = found.setdefault(
        owner, {"deployment": deployment, "initContainers": 0, "containers": 0}
    )

    # Positive, name-keyed assertion on the application container itself, so a
    # non-`langfuse/` image there cannot slip past the prefix rule below. Any
    # `langfuse/` image is already covered (and counted) by that rule.
    owner_container = next(
        container for container in sections["containers"] if container.get("name") == owner
    )
    owner_image = owner_container.get("image")
    if not str(owner_image).startswith(LANGFUSE_IMAGE_PREFIX):
        seen["containers"] += 1
        problems.append(
            f"chart {deployment} container {owner} must use reviewed image {wanted}; "
            f"found {owner_image!r} (floating Langfuse tags are forbidden)"
        )

    for section, container in langfuse_refs:
        seen[section] += 1
        actual = container.get("image")
        if actual != wanted:
            problems.append(
                f"chart {deployment} {section[:-1]} {container.get('name')} must use "
                f"reviewed image {wanted}; found {actual!r} "
                "(floating Langfuse tags are forbidden)"
            )

for name, wanted in expected.items():
    if name not in found:
        problems.append(
            f"chart renders no {name} container at all; expected {wanted} "
            "(floating Langfuse tags are forbidden)"
        )
        continue
    for section in SECTIONS:
        if not found[name][section]:
            problems.append(
                f"chart {found[name]['deployment']} has no {section[:-1]} running a "
                f"{LANGFUSE_IMAGE_PREFIX}* image, so the pin check over that section is "
                f"vacuous; expected {wanted} (floating Langfuse tags are forbidden)"
            )

compose = json.loads(pathlib.Path(compose_path).read_text())
compose_images = {
    name: compose.get("services", {}).get(name, {}).get("image")
    for name in expected
}
for name, wanted in expected.items():
    actual = compose_images.get(name)
    if actual != wanted:
        problems.append(
            f"compose {name} must use reviewed image {wanted}; found {actual!r} "
            "(floating Langfuse tags are forbidden)"
        )

if problems:
    raise SystemExit("\n".join(problems))

checked = sum(found[name][section] for name in expected for section in SECTIONS)
print(
    f"chart pins {checked} Langfuse image references (containers + initContainers) "
    f"and Compose pins web/worker to {expected_version}"
)
PY

python3 "$CHECKER" "$RENDER" "$COMPOSE_JSON" "$EXPECTED_VERSION"

# Negative control 1: a floating tag on every surface is rejected.
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

# Negative control 2: the initContainer coverage is not decorative. Float ONLY
# the initContainer images, leaving both application containers and the whole
# Compose surface correctly pinned, and require the gate to still fail.
INIT_MUTANT_RENDER="$TMP/chart-floating-init.yaml"
python3 - "$RENDER" "$INIT_MUTANT_RENDER" <<'PY'
import pathlib
import sys

import yaml

render, mutant_render = sys.argv[1:]
documents = [
    document
    for document in yaml.safe_load_all(pathlib.Path(render).read_text())
    if document
]
mutated = 0
for document in documents:
    if document.get("kind") != "Deployment":
        continue
    pod = document.get("spec", {}).get("template", {}).get("spec", {})
    for container in pod.get("initContainers") or []:
        if str(container.get("image", "")).startswith("langfuse/"):
            container["image"] = container["image"].rsplit(":", 1)[0] + ":3"
            mutated += 1
if mutated != 2:
    raise SystemExit(
        f"expected 2 Langfuse initContainer images to mutate, mutated {mutated}"
    )
pathlib.Path(mutant_render).write_text(yaml.safe_dump_all(documents))
PY

init_negative_output=""
if init_negative_output="$(python3 "$CHECKER" "$INIT_MUTANT_RENDER" "$COMPOSE_JSON" "$EXPECTED_VERSION" 2>&1)"; then
  echo "FAIL: floating initContainer image passed the Langfuse image contract" >&2
  exit 1
fi
if [[ "$init_negative_output" != *" initContainer "* ]]; then
  echo "FAIL: initContainer mutation failed for the wrong reason: $init_negative_output" >&2
  exit 1
fi
if [[ "$init_negative_output" == *" container langfuse-"* ]]; then
  echo "FAIL: initContainer mutation also tripped an application-container check, so it does not isolate the initContainer coverage: $init_negative_output" >&2
  exit 1
fi

echo "negative: a floating :3 image in an initContainer position is rejected"
echo "PASS: chart and Compose share one reviewed Langfuse version and reject floating tags"
