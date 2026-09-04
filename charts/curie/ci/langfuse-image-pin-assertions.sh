#!/usr/bin/env bash
#
# Issue #2190: keep the shipped Langfuse runtime on one reviewed version.
#
# A floating `:3` moved from 3.225.5 to 3.225.6 during one CI run. The newly
# pulled image left ClickHouse migration 39 dirty in the real delayed-Postgres
# chart consumer and made the Compose observability query return 500. This gate
# renders both user-facing consumers, requires one exact version, and proves a
# floating-tag mutation is rejected.
#
# Issue #2332: the same three images must also be pinnable by digest. The
# templates used to concatenate `repo` and `tag` with a colon, so an operator
# who set `repo@sha256:...` got `repo@sha256:...:tag` and the kubelet refused it
# with InvalidImageName. The second half of this file renders the chart with
# digests set and proves every one of those images -- including the
# wait-for-clickhouse init container that gates them -- resolves to a bare
# `repo@sha256:...` and never to the `@sha256:...:tag` shape.
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

# ---------------------------------------------------------------------------
# Issue #2332: digest-pinnability. Chart render only -- no Compose -- so this
# section stays runnable wherever `docker compose` is unavailable.
# ---------------------------------------------------------------------------
WEB_DIGEST="sha256:$(printf 'aa%.0s' $(seq 32))"
WORKER_DIGEST="sha256:$(printf 'bb%.0s' $(seq 32))"
CLICKHOUSE_DIGEST="sha256:$(printf 'cc%.0s' $(seq 32))"
CLICKHOUSE_TAG="25.12"  # charts/curie/values.yaml clickhouse.image.tag

DIGEST_RENDER="$TMP/chart-digest.yaml"
DIGEST_CHECKER="$TMP/check-digest.py"

helm template curie "$CHART" \
  --set-string "langfuse.image.webDigest=$WEB_DIGEST" \
  --set-string "langfuse.image.workerDigest=$WORKER_DIGEST" \
  --set-string "clickhouse.image.digest=$CLICKHOUSE_DIGEST" \
  >"$DIGEST_RENDER"

cat >"$DIGEST_CHECKER" <<'PY'
import pathlib
import re
import sys

import yaml

render_path, web_digest, worker_digest, clickhouse_digest = sys.argv[1:]

expected = {
    "langfuse-web": f"langfuse/langfuse@{web_digest}",
    "langfuse-worker": f"langfuse/langfuse-worker@{worker_digest}",
    "clickhouse": f"clickhouse/clickhouse-server@{clickhouse_digest}",
}

documents = [
    document
    for document in yaml.safe_load_all(pathlib.Path(render_path).read_text())
    if document
]

app_images = {}
# Workload container name -> (app container image, wait-for-clickhouse image).
gate_pairs = {}
all_images = []
preflight_env = None  # {"CLICKHOUSE_IMAGE": ..., "CLICKHOUSE_TAG": ...} from the AVX preflight Job.
for document in documents:
    spec = document.get("spec")
    pod_spec = spec.get("template", {}).get("spec", {}) if isinstance(spec, dict) else {}
    containers = pod_spec.get("containers") or []
    init_containers = pod_spec.get("initContainers") or []
    for container in containers + init_containers:
        all_images.append(container.get("image"))
    gate = next((c for c in init_containers if c.get("name") == "wait-for-clickhouse"), None)
    for container in containers:
        name = container.get("name")
        if name in expected:
            app_images[name] = container.get("image")
            if gate is not None:
                gate_pairs[name] = (container.get("image"), gate.get("image"))
        env_pairs = {e.get("name"): e.get("value") for e in (container.get("env") or [])}
        if "CLICKHOUSE_IMAGE" in env_pairs:
            preflight_env = env_pairs

problems = []
for name, wanted in expected.items():
    actual = app_images.get(name)
    if actual != wanted:
        problems.append(
            f"{name} must render digest reference {wanted}; found {actual!r} "
            "(digest pinning is broken)"
        )

for name in ("langfuse-web", "langfuse-worker"):
    if name not in gate_pairs:
        problems.append(
            f"{name} has no wait-for-clickhouse init container to compare "
            "(digest pinning is broken)"
        )
        continue
    app_image, gate_image = gate_pairs[name]
    if gate_image != app_image:
        problems.append(
            f"{name} wait-for-clickhouse init container must use the same bytes as the "
            f"app container {app_image!r}; found {gate_image!r} (digest pinning is broken)"
        )

if preflight_env is None:
    problems.append(
        "no Job container env carries CLICKHOUSE_IMAGE to compare against the AVX "
        "preflight (digest pinning is broken)"
    )
else:
    clickhouse_app_image = app_images.get("clickhouse")
    if preflight_env.get("CLICKHOUSE_IMAGE") != clickhouse_app_image:
        problems.append(
            "AVX preflight Job env CLICKHOUSE_IMAGE must use the same bytes as the "
            f"ClickHouse container {clickhouse_app_image!r}; found "
            f"{preflight_env.get('CLICKHOUSE_IMAGE')!r} (digest pinning is broken)"
        )
    if "@sha256:" in (preflight_env.get("CLICKHOUSE_TAG") or ""):
        problems.append(
            "AVX preflight Job env CLICKHOUSE_TAG must stay a plain version string "
            f"for the SSE4.2 prefix match, not a digest; found "
            f"{preflight_env.get('CLICKHOUSE_TAG')!r} (digest pinning is broken)"
        )

invalid = sorted(
    {image for image in all_images if image and re.search(r"@sha256:[0-9a-f]+:", image)}
)
if invalid:
    problems.append(
        "rendered images use the InvalidImageName shape @sha256:...:tag: "
        + ", ".join(invalid)
        + " (digest pinning is broken)"
    )

if problems:
    raise SystemExit("\n".join(problems))

print("chart renders every pinnable image as a bare repo@sha256 reference")
PY

python3 "$DIGEST_CHECKER" "$DIGEST_RENDER" "$WEB_DIGEST" "$WORKER_DIGEST" "$CLICKHOUSE_DIGEST"
echo "digest: langfuse web/worker, clickhouse and the clickhouse gate all pin by digest"
echo "digest: AVX preflight Job CLICKHOUSE_IMAGE matches the ClickHouse digest while CLICKHOUSE_TAG stays a plain version string"

MUTANT_DIGEST_RENDER="$TMP/chart-digest-suffixed.yaml"
python3 - "$DIGEST_RENDER" "$MUTANT_DIGEST_RENDER" "$EXPECTED_VERSION" "$CLICKHOUSE_TAG" <<'PY'
import pathlib
import re
import sys

render, mutant, langfuse_version, clickhouse_tag = sys.argv[1:]

# Reproduce exactly what the broken templates rendered: repository, digest, then
# a concatenated `:tag` the kubelet rejects as InvalidImageName.
text = pathlib.Path(render).read_text()
text = re.sub(
    r"(langfuse/langfuse(?:-worker)?@sha256:[0-9a-f]+)",
    lambda match: f"{match.group(1)}:{langfuse_version}",
    text,
)
text = re.sub(
    r"(clickhouse/clickhouse-server@sha256:[0-9a-f]+)",
    lambda match: f"{match.group(1)}:{clickhouse_tag}",
    text,
)
pathlib.Path(mutant).write_text(text)
PY

digest_negative_output=""
if digest_negative_output="$(python3 "$DIGEST_CHECKER" "$MUTANT_DIGEST_RENDER" "$WEB_DIGEST" "$WORKER_DIGEST" "$CLICKHOUSE_DIGEST" 2>&1)"; then
  echo "FAIL: an @sha256:...:tag render passed the digest-pin contract" >&2
  exit 1
fi
if [[ "$digest_negative_output" != *"digest pinning is broken"* ]]; then
  echo "FAIL: digest mutation failed unexpectedly: $digest_negative_output" >&2
  exit 1
fi

echo "negative: appending :<tag> after a digest is rejected"

python3 - "$RENDER" "$CLICKHOUSE_TAG" <<'PY'
import pathlib
import sys

import yaml

render_path, clickhouse_tag = sys.argv[1:]
wanted = f"clickhouse/clickhouse-server:{clickhouse_tag}"

actual = None
for document in yaml.safe_load_all(pathlib.Path(render_path).read_text()):
    if not document or not isinstance(document.get("spec"), dict):
        continue
    containers = document["spec"].get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        if container.get("name") == "clickhouse":
            actual = container.get("image")

if actual != wanted:
    raise SystemExit(
        f"with no digest set the clickhouse container must stay on {wanted}; found {actual!r}"
    )

print(f"default render keeps ClickHouse on {wanted}")
PY

echo "default: no digest set leaves the reviewed ClickHouse tag path unchanged"
echo "PASS: chart and Compose share one reviewed Langfuse version, reject floating tags, and every pinnable image renders a valid digest reference"
