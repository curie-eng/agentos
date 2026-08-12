#!/usr/bin/env bash
#
# The API and the worker must agree about the object store.
#
# The API WRITES each uploaded plugin bundle to the bundle bucket; the worker
# READS it back to run a turn. If the two disagree -- or if either is simply
# unset -- the write and the read address different stores and the bundle is
# unfetchable.
#
# This shipped: worker.yaml set none of the four variables, so the worker fell
# back to its compose defaults (`s3_endpoint_url` = http://localhost:29000) and
# every bundle fetch in Kubernetes hit ECONNREFUSED.
#
# It is asserted here rather than left to review because both symptoms point
# somewhere else:
#
#   * `eval default @ <sha> failed: unresolvable suite/bundle` -- the
#     post-deploy eval silently stops running. A suite that cannot resolve looks
#     exactly like a suite nobody asked for, so nothing alerts and the gap can
#     last for weeks.
#   * every turn fails as `ClaimTimeoutError` after 90s, and that message names
#     a CPU-saturated node as the most common cause. On the install where this
#     was found the node was at 11% CPU with 4% pressure -- the error sent
#     everyone looking at node size instead of at four missing env vars.
#
# A unit test cannot catch it: each Deployment is correct in isolation, and the
# defect is only visible when the two are compared.
set -euo pipefail

CHART=${CHART:-charts/curie}
KEYS=(S3_ENDPOINT_URL S3_ACCESS_KEY S3_SECRET_KEY BUNDLE_BUCKET)

# Render to a FILE. Passing both a script heredoc and the rendered manifests on
# stdin means two redirections on one command, and the second wins -- python
# then tries to execute the YAML. That cost a debugging round here already.
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
helm template t "$CHART" > "$rendered"

python3 - "$rendered" "${KEYS[@]}" <<'PY'
import sys, yaml

path, keys = sys.argv[1], sys.argv[2:]
with open(path) as fh:
    docs = list(yaml.safe_load_all(fh))

def env_of(component):
    """Select by the component LABEL, never by a name suffix.

    `endswith("-worker")` also matches `<release>-curie-langfuse-worker`, which
    renders earlier and legitimately has no object-store env -- so a suffix
    match silently inspected Langfuse and reported the curie worker as
    misconfigured while the chart was correct. The label is unambiguous.
    """

    for d in docs:
        if not d or d.get("kind") != "Deployment":
            continue
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") != component:
            continue
        c = d["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e for e in c.get("env", [])}
    return None

api = env_of("api")
worker = env_of("worker")
assert api is not None, "no API Deployment rendered"
assert worker is not None, "no worker Deployment rendered"

failures = []

for k in keys:
    if k not in worker:
        failures.append(f"worker is missing {k} -- it will fall back to the compose default")
    if k not in api:
        failures.append(f"api is missing {k}")

# Equality, not merely presence. Two different-but-present endpoints is the
# subtler version of the same bug and would pass a presence-only check.
for k in keys:
    if k in api and k in worker and api[k] != worker[k]:
        failures.append(
            f"api and worker disagree on {k}:\n"
            f"    api    = {api[k]}\n"
            f"    worker = {worker[k]}"
        )

# The compose default must never be what a Kubernetes pod receives.
ep = worker.get("S3_ENDPOINT_URL", {}).get("value", "")
if "localhost" in ep or "127.0.0.1" in ep:
    failures.append(f"worker S3_ENDPOINT_URL points at the pod itself: {ep!r}")

# The credential belongs in a secretKeyRef, never inline in the pod spec, where
# `kubectl get deploy -o yaml` would print it to anyone who can read Deployments.
sk = worker.get("S3_SECRET_KEY", {})
if "value" in sk:
    failures.append("worker S3_SECRET_KEY is an inline value; it must be a secretKeyRef")
elif not sk.get("valueFrom", {}).get("secretKeyRef"):
    failures.append("worker S3_SECRET_KEY has no secretKeyRef")

if failures:
    print("FAIL: api/worker object-store configuration diverges\n")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)

print("api and worker agree on all four object-store variables:")
for k in keys:
    shown = api[k].get("value") or "<secretKeyRef " + \
        api[k]["valueFrom"]["secretKeyRef"]["name"] + "/" + \
        api[k]["valueFrom"]["secretKeyRef"]["key"] + ">"
    print(f"  {k:18} = {shown}")
PY
