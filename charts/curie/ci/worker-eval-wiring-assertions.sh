#!/usr/bin/env bash
#
# A default worker must receive the platform API and Langfuse settings needed
# to report its eval results. Rendering both connector states prevents a
# conditional API include from leaving one state unwired or duplicating entries.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

helm template t "$CHART" > "$TMP/default.yaml"
helm template t "$CHART" \
  --set worker.connectorReconciler.enabled=true > "$TMP/connector.yaml"

python3 - "$TMP/default.yaml" "$TMP/connector.yaml" <<'PY'
import sys

import yaml


EXPECTED = {
    "CURIE_API_URL": {
        "name": "CURIE_API_URL",
        "value": "http://t-curie-api:8000",
    },
    "CURIE_API_KEY": {
        "name": "CURIE_API_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "t-curie-secrets",
                "key": "apiKey",
            },
        },
    },
    "LANGFUSE_HOST": {
        "name": "LANGFUSE_HOST",
        "value": "http://t-curie-langfuse-web:3000",
    },
    "LANGFUSE_PUBLIC_KEY": {
        "name": "LANGFUSE_PUBLIC_KEY",
        "value": "pk-lf-curie-dev",
    },
    "LANGFUSE_SECRET_KEY": {
        "name": "LANGFUSE_SECRET_KEY",
        "valueFrom": {
            "secretKeyRef": {
                "name": "t-curie-secrets",
                "key": "langfuseInitProjectSecretKey",
            },
        },
    },
}


def failures_for(path, render):
    with open(path) as handle:
        docs = [doc for doc in yaml.safe_load_all(handle) if doc]

    workers = [
        doc
        for doc in docs
        if doc.get("kind") == "Deployment"
        and (doc.get("metadata") or {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        )
        == "worker"
    ]
    if len(workers) != 1:
        return [
            "%s render found %d worker Deployments by component label, expected exactly one"
            % (render, len(workers))
        ]

    containers = (
        (workers[0].get("spec") or {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if not containers:
        return ["%s worker Deployment has no containers" % render]

    env = containers[0].get("env", [])
    if not isinstance(env, list):
        return ["%s worker container env is not a list" % render]

    failures = []
    for name, expected in EXPECTED.items():
        entries = [entry for entry in env if isinstance(entry, dict) and entry.get("name") == name]
        if len(entries) != 1:
            failures.append(
                "%s worker has %d %s entries, expected exactly one"
                % (render, len(entries), name)
            )
            continue
        if entries[0] != expected:
            failures.append(
                "%s worker %s is %r, expected %r"
                % (render, name, entries[0], expected)
            )
    return failures


failures = []
for path, render in zip(sys.argv[1:], ("default", "connector reconciler enabled")):
    failures.extend(failures_for(path, render))

if failures:
    print("FAIL: worker eval wiring render assertions failed", file=sys.stderr)
    for failure in failures:
        print("  " + failure, file=sys.stderr)
    raise SystemExit(1)

print("OK: worker eval wiring render assertions passed")
PY
