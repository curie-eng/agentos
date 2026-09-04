#!/usr/bin/env bash
#
# Issue #2318: every image the chart names must resolve to the same bytes for
# the life of a chart version. A values *Image key or a rendered container
# image with no tag, or with tag `latest`, pulls whatever that repository's
# :latest is that day and (for :latest / untagged) defaults imagePullPolicy
# to Always. The security probe's netshoot image was the first miss: a bare
# `nicolaka/netshoot` became the body of four helm-test pods.
#
# Proves:
#   1. DEFAULT values and the default helm template have no untagged or
#      :latest *Image / container image references.
#   2. NEGATIVE: an untagged *Image value is refused.
#   3. NEGATIVE: a :latest *Image value is refused.
#   4. NEGATIVE: a rendered container image mutated to drop its tag, or to
#      :latest, is refused.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

CHECKER="$TMP/check.py"
cat >"$CHECKER" <<'PY'
import pathlib
import sys

import yaml

UNPINNED_PREFIX = "unpinned image:"


def last_name_component(image):
    # Registry hosts may carry a port (`localhost:5000/foo`), so the tag lives
    # on the last path component, not the first colon in the string.
    return image.split("/")[-1]


def unpinned_reason(image):
    image = (image or "").strip()
    if not image:
        return None
    if "@" in image:
        name, digest = image.rsplit("@", 1)
        if digest.startswith("sha256:") and digest[len("sha256:") :]:
            return None
        return "malformed digest"
    name = last_name_component(image)
    if ":" not in name:
        return "no tag"
    tag = name.rsplit(":", 1)[1]
    if tag == "":
        return "empty tag"
    if tag == "latest":
        return "tag is latest"
    return None


def walk_image_keys(obj, path, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if key.endswith("Image") and isinstance(value, str) and value.strip():
                found.append((child, value.strip()))
            walk_image_keys(value, child, found)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            walk_image_keys(value, f"{path}[{index}]", found)


def is_container_like(mapping):
    if not isinstance(mapping, dict):
        return False
    if not isinstance(mapping.get("image"), str):
        return False
    return any(
        field in mapping
        for field in (
            "name",
            "command",
            "args",
            "env",
            "ports",
            "resources",
            "securityContext",
            "workingDir",
            "volumeMounts",
        )
    )


def walk_container_images(obj, path, found):
    if is_container_like(obj):
        image = obj["image"].strip()
        if image:
            name = obj.get("name") or path
            found.append((f"{path} ({name})", image))
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            walk_container_images(value, child, found)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            walk_container_images(value, f"{path}[{index}]", found)


def collect_values_images(values_paths):
    found = []
    for values_path in values_paths:
        data = yaml.safe_load(pathlib.Path(values_path).read_text()) or {}
        walk_image_keys(data, pathlib.Path(values_path).name, found)
    return found


def collect_render_images(render_path):
    found = []
    documents = yaml.safe_load_all(pathlib.Path(render_path).read_text())
    for document in documents:
        if not document:
            continue
        kind = document.get("kind") or "unknown"
        name = (document.get("metadata") or {}).get("name") or "unnamed"
        walk_container_images(document, f"{kind}/{name}", found)
    return found


def problems_for(refs):
    problems = []
    for where, image in refs:
        reason = unpinned_reason(image)
        if reason:
            problems.append(f"{UNPINNED_PREFIX} {image} ({where}: {reason})")
    return problems


def main(argv):
    if len(argv) < 3:
        raise SystemExit("usage: check.py <render.yaml> <values.yaml> [more values files...]")
    render_path = argv[1]
    values_paths = argv[2:]
    problems = problems_for(collect_values_images(values_paths))
    problems.extend(problems_for(collect_render_images(render_path)))
    if problems:
        raise SystemExit("\n".join(problems))
    print("ok: no untagged or :latest *Image values or rendered container images")


if __name__ == "__main__":
    main(sys.argv)
PY

VALUES_FILES=("$CHART/values.yaml")
for overlay in "$CHART"/values-*.yaml; do
  if [[ -f "$overlay" ]]; then
    VALUES_FILES+=("$overlay")
  fi
done

RENDER="$TMP/chart.yaml"
echo "=== Rendering chart (default values) ==="
helm template curie "$CHART" >"$RENDER"

echo "=== Default values *Image keys and rendered container images ==="
if ! default_out="$(python3 "$CHECKER" "$RENDER" "${VALUES_FILES[@]}" 2>&1)"; then
  fail "default chart carries an untagged or :latest image: $default_out"
fi
echo "  $default_out"

echo "=== Negative: untagged *Image value is refused ==="
UNTAGGED_VALUES="$TMP/values-untagged.yaml"
python3 - "$CHART/values.yaml" "$UNTAGGED_VALUES" <<'PY'
import pathlib
import re
import sys

src, dest = sys.argv[1:]
text = pathlib.Path(src).read_text()
updated, count = re.subn(
    r"^(\s*netshootImage:\s*)\S+\s*$",
    r"\1nicolaka/netshoot",
    text,
    count=1,
    flags=re.M,
)
if count != 1:
    raise SystemExit("expected one netshootImage assignment in values.yaml")
pathlib.Path(dest).write_text(updated)
PY
untagged_out=""
if untagged_out="$(python3 "$CHECKER" "$RENDER" "$UNTAGGED_VALUES" 2>&1)"; then
  fail "untagged netshootImage mutation passed the unpinned-image contract"
fi
if [[ "$untagged_out" != *"unpinned image: nicolaka/netshoot ("* || "$untagged_out" != *": no tag)"* ]]; then
  fail "untagged mutation failed unexpectedly: $untagged_out"
fi
echo "  ok: untagged netshootImage is rejected"

echo "=== Negative: :latest *Image value is refused ==="
LATEST_VALUES="$TMP/values-latest.yaml"
python3 - "$CHART/values.yaml" "$LATEST_VALUES" <<'PY'
import pathlib
import re
import sys

src, dest = sys.argv[1:]
text = pathlib.Path(src).read_text()
updated, count = re.subn(
    r"^(\s*netshootImage:\s*)\S+\s*$",
    r"\1nicolaka/netshoot:latest",
    text,
    count=1,
    flags=re.M,
)
if count != 1:
    raise SystemExit("expected one netshootImage assignment in values.yaml")
pathlib.Path(dest).write_text(updated)
PY
latest_out=""
if latest_out="$(python3 "$CHECKER" "$RENDER" "$LATEST_VALUES" 2>&1)"; then
  fail ":latest netshootImage mutation passed the unpinned-image contract"
fi
if [[ "$latest_out" != *"unpinned image: nicolaka/netshoot:latest"* ]]; then
  fail ":latest mutation failed unexpectedly: $latest_out"
fi
echo "  ok: netshootImage:latest is rejected"

echo "=== Negative: rendered container image with no tag is refused ==="
NOTAG_RENDER="$TMP/chart-notag.yaml"
python3 - "$RENDER" "$NOTAG_RENDER" <<'PY'
import pathlib
import sys

import yaml

src, dest = sys.argv[1:]
documents = list(yaml.safe_load_all(pathlib.Path(src).read_text()))
mutated = False
for document in documents:
    if not document:
        continue
    spec = (document.get("spec") or {}).get("template", {}).get("spec") or {}
    for container in spec.get("containers") or []:
        image = container.get("image") or ""
        if image == "busybox:1.36":
            container["image"] = "busybox"
            mutated = True
            break
    if mutated:
        break
if not mutated:
    raise SystemExit("could not find a rendered busybox:1.36 container to drop the tag from")
with pathlib.Path(dest).open("w") as handle:
    yaml.safe_dump_all(documents, handle)
PY
notag_out=""
if notag_out="$(python3 "$CHECKER" "$NOTAG_RENDER" "$CHART/values.yaml" 2>&1)"; then
  fail "rendered no-tag mutation passed the unpinned-image contract"
fi
if [[ "$notag_out" != *"unpinned image: busybox ("* || "$notag_out" != *": no tag)"* ]]; then
  fail "rendered no-tag mutation failed unexpectedly: $notag_out"
fi
echo "  ok: rendered busybox with no tag is rejected"

echo "=== Negative: rendered :latest container image is refused ==="
LATEST_RENDER="$TMP/chart-latest.yaml"
python3 - "$RENDER" "$LATEST_RENDER" <<'PY'
import pathlib
import sys

import yaml

src, dest = sys.argv[1:]
documents = list(yaml.safe_load_all(pathlib.Path(src).read_text()))
mutated = False
for document in documents:
    if not document:
        continue
    spec = (document.get("spec") or {}).get("template", {}).get("spec") or {}
    for container in spec.get("containers") or []:
        image = container.get("image") or ""
        if image == "busybox:1.36":
            container["image"] = "busybox:latest"
            mutated = True
            break
    if mutated:
        break
if not mutated:
    raise SystemExit("could not find a rendered busybox:1.36 container to retag as :latest")
with pathlib.Path(dest).open("w") as handle:
    yaml.safe_dump_all(documents, handle)
PY
latest_render_out=""
if latest_render_out="$(python3 "$CHECKER" "$LATEST_RENDER" "$CHART/values.yaml" 2>&1)"; then
  fail "rendered :latest mutation passed the unpinned-image contract"
fi
if [[ "$latest_render_out" != *"unpinned image: busybox:latest"* ]]; then
  fail "rendered :latest mutation failed unexpectedly: $latest_render_out"
fi
echo "  ok: rendered busybox:latest is rejected"

echo
echo "PASS: values *Image keys and rendered container images are pinned; untagged and :latest mutations are rejected"
