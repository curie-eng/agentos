#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

render() {
  local name="$1"
  shift
  helm template curie "$CHART" --output-dir "$TMP/$name" "$@" >/dev/null
}

manifest_for() {
  local manifest
  manifest="$(find "$1" -type f -path '*/templates/otel-collector.yaml' -print -quit)"
  [[ -n "$manifest" ]] || fail "OTel Collector manifest was not written to $1"
  printf '%s\n' "$manifest"
}

render default
render port-3001 --set langfuse.web.service.port=3001
render auth-header --set otelCollector.otlpAuthHeader='Basic YXNzZXJ0OmFzc2VydA=='

python3 - "$(manifest_for "$TMP/default")" "$(manifest_for "$TMP/port-3001")" "$(manifest_for "$TMP/auth-header")" <<'PY'
import sys
import yaml

def config_and_checksum(path):
    docs = [doc for doc in yaml.safe_load_all(open(path)) if doc]
    config_map = next((doc for doc in docs if doc.get("kind") == "ConfigMap"), None)
    deployment = next((doc for doc in docs if doc.get("kind") == "Deployment"), None)
    assert config_map is not None, f"{path}: no ConfigMap rendered"
    assert deployment is not None, f"{path}: no Deployment rendered"
    config = config_map.get("data", {}).get("collector-config.yaml")
    checksum = deployment["spec"]["template"]["metadata"].get("annotations", {}).get("checksum/config")
    assert config, f"{path}: collector ConfigMap body was not rendered"
    assert checksum, f"{path}: Deployment checksum/config was not rendered"
    return config, checksum

default_config, default_checksum = config_and_checksum(sys.argv[1])
changed_config, changed_checksum = config_and_checksum(sys.argv[2])
auth_config, auth_checksum = config_and_checksum(sys.argv[3])

assert default_config != changed_config, "Langfuse service port did not change the collector ConfigMap body"
assert default_checksum != changed_checksum, (
    "ConfigMap body changed but Deployment checksum/config stayed identical. The pod would not roll."
)
assert default_config == auth_config, "OTLP auth header changed the collector ConfigMap body"
assert default_checksum != auth_checksum, (
    "OTLP auth header changed but Deployment checksum/config stayed identical. The pod would not roll."
)

print("ConfigMap changes roll the pod and OTLP auth header changes still roll it: OK")
PY
