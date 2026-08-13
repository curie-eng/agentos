#!/usr/bin/env bash
#
# The API WRITES each uploaded bundle to the object store and the worker READS it
# back. If the two disagree about where that store is, the write succeeds, the
# read fails, and the failure surfaces nowhere near its cause.
#
# This was a real outage. worker.yaml set none of the four variables, so the
# worker fell back to its compose default (http://localhost:29000) and every
# bundle fetch got ECONNREFUSED. Both symptoms pointed elsewhere:
#
#   * every Slack turn died as ClaimTimeoutError after 90s, and that message
#     names a CPU-saturated node first -- on an install idling at 11% CPU
#   * the post-deploy eval silently stopped running with "unresolvable
#     suite/bundle", which alerts nothing, because a suite that cannot resolve
#     looks exactly like a suite nobody asked for
#
# So this asserts AGREEMENT, not presence. Presence alone would still pass the
# day someone points the worker at a different-but-populated endpoint.
set -euo pipefail

CHART=${CHART:-charts/curie}
DEFAULT_RENDERED=$(mktemp)
EXTERNAL_RENDERED=$(mktemp)
EXTERNAL_VALUES=$(mktemp)
trap 'rm -f "$DEFAULT_RENDERED" "$EXTERNAL_RENDERED" "$EXTERNAL_VALUES"' EXIT

cat > "$EXTERNAL_VALUES" <<'EOF'
rustfs:
  deploy: false
  host: s3.example.com
  port: 443
EOF

helm template curie "$CHART" --namespace dev > "$DEFAULT_RENDERED"
helm template curie "$CHART" --namespace dev \
  --values "$EXTERNAL_VALUES" > "$EXTERNAL_RENDERED"

# The rendered manifests go via a FILE, not a second stdin redirect: with both
# `<<'PY'` and `<<<"$out"` on one command the herestring wins and python reads
# the chart instead of the script.
python3 - \
  "$DEFAULT_RENDERED" "default" "http://curie-rustfs:9000" \
  "$EXTERNAL_RENDERED" "external" "https://s3.example.com:443" <<'PY'
import sys, yaml

KEYS = ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "BUNDLE_BUCKET")


def env_for(container):
    return {entry["name"]: entry for entry in container.get("env", [])}


def object_store_envs(path):
    api = worker = bundle_fetch = None
    with open(path) as fh:
        for doc in yaml.safe_load_all(fh):
            if not doc:
                continue
            if doc.get("kind") == "Deployment":
                name = doc["metadata"]["name"]
                containers = doc["spec"]["template"]["spec"].get("containers", [])
                assert containers, f"{name} Deployment has no containers"
                if name.endswith("-api"):
                    api = env_for(containers[0])
                elif name.endswith("-worker"):
                    worker = env_for(containers[0])
            elif doc.get("kind") == "SandboxTemplate":
                pod_spec = doc["spec"]["podTemplate"]["spec"]
                matches = [
                    container
                    for container in pod_spec.get("initContainers", [])
                    if container.get("name") == "bundle-fetch"
                ]
                assert len(matches) == 1, (
                    "SandboxTemplate must render exactly one bundle-fetch init container"
                )
                bundle_fetch = env_for(matches[0])

    assert api is not None, "api Deployment not rendered"
    assert worker is not None, "worker Deployment not rendered"
    assert bundle_fetch is not None, "SandboxTemplate bundle-fetch not rendered"
    return api, worker, bundle_fetch


def assert_contract(path, label, expected_endpoint):
    api, worker, bundle_fetch = object_store_envs(path)

    for service, env in (("api", api), ("worker", worker)):
        missing = [key for key in KEYS if key not in env]
        if service == "worker":
            assert not missing, (
                f"worker is missing {missing}. Its config defaults these to the compose stack "
                "(http://localhost:29000), so every bundle fetch fails with ECONNREFUSED -- and "
                "the symptom is a ClaimTimeoutError that blames the node's CPU."
            )
        else:
            assert not missing, f"{service} is missing object-store keys: {missing}"

    for key in KEYS:
        assert api[key] == worker[key], (
            f"api and worker disagree on {key}:\n  api    = {api[key]}\n  worker = {worker[key]}\n"
            "The API writes bundles and the worker reads them, so a difference here means "
            "the write lands somewhere the read never looks."
        )

    assert api["S3_ENDPOINT_URL"].get("value") == expected_endpoint, (
        f"{label} API endpoint must be {expected_endpoint}, got {api['S3_ENDPOINT_URL']}"
    )
    assert worker["S3_ENDPOINT_URL"].get("value") == expected_endpoint, (
        f"{label} worker endpoint must be {expected_endpoint}, got {worker['S3_ENDPOINT_URL']}"
    )
    assert bundle_fetch.get("S3_ENDPOINT", {}).get("value") == expected_endpoint, (
        f"{label} bundle-fetch endpoint must be {expected_endpoint}, "
        f"got {bundle_fetch.get('S3_ENDPOINT')}"
    )

    # The credential must be a reference, never inline: helm keeps its values in the
    # release Secret, so an inline password is readable in every retained revision.
    entry = worker["S3_SECRET_KEY"]
    ref = entry.get("valueFrom", {}).get("secretKeyRef")
    assert ref, "S3_SECRET_KEY must come from a secretKeyRef"
    assert "value" not in entry, "S3_SECRET_KEY must not be an inline value"

    print(f"ok: {label}: api and worker agree on {', '.join(KEYS)}")
    print(f"ok: {label}: all S3 endpoints are {expected_endpoint}")
    print(f"ok: {label}: S3_SECRET_KEY is a ref -> {ref['name']}/{ref['key']}")


arguments = sys.argv[1:]
assert len(arguments) % 3 == 0, "expected path, label, endpoint triples"
for index in range(0, len(arguments), 3):
    assert_contract(*arguments[index:index + 3])
PY
