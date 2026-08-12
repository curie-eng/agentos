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
RENDERED=$(mktemp); trap 'rm -f "$RENDERED"' EXIT
helm template t "$CHART" > "$RENDERED"

# The rendered manifests go via a FILE, not a second stdin redirect: with both
# `<<'PY'` and `<<<"$out"` on one command the herestring wins and python reads
# the chart instead of the script.
python3 - "$RENDERED" <<'PY'
import sys, yaml

api = worker = None
with open(sys.argv[1]) as fh:
    for doc in yaml.safe_load_all(fh):
        if not doc or doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        env = {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        if name.endswith("-api"):
            api = env
        elif name.endswith("-worker"):
            worker = env

assert api is not None, "api Deployment not rendered"
assert worker is not None, "worker Deployment not rendered"

KEYS = ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "BUNDLE_BUCKET")

missing = [k for k in KEYS if k not in worker]
assert not missing, (
    f"worker is missing {missing}. Its config defaults these to the compose stack "
    "(http://localhost:29000), so every bundle fetch fails with ECONNREFUSED -- and "
    "the symptom is a ClaimTimeoutError that blames the node's CPU."
)

for k in KEYS:
    assert api[k] == worker[k], (
        f"api and worker disagree on {k}:\n  api    = {api[k]}\n  worker = {worker[k]}\n"
        "The API writes bundles and the worker reads them, so a difference here means "
        "the write lands somewhere the read never looks."
    )

# The credential must be a reference, never inline: helm keeps its values in the
# release Secret, so an inline password is readable in every retained revision.
entry = worker["S3_SECRET_KEY"]
ref = entry.get("valueFrom", {}).get("secretKeyRef")
assert ref, "S3_SECRET_KEY must come from a secretKeyRef"
assert "value" not in entry, "S3_SECRET_KEY must not be an inline value"

print(f"ok: api and worker agree on {', '.join(KEYS)}")
print(f"ok: S3_SECRET_KEY is a ref -> {ref['name']}/{ref['key']}")
PY
