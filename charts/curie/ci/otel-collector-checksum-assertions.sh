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
render tempo \
  --set 'otelCollector.extraExporters.otlphttp/tempo.endpoint=http://tempo:4318' \
  --set 'otelCollector.extraPipelineExporters[0]=otlphttp/tempo'

missing_exporter_stderr="$TMP/missing-exporter.stderr"
if helm template curie "$CHART" \
  --set 'otelCollector.extraPipelineExporters[0]=otlphttp/tempo' \
  >/dev/null 2>"$missing_exporter_stderr"; then
  fail "Helm accepted missing exporter otlphttp/tempo; add it under otelCollector.extraExporters"
fi
missing_exporter_error="$(<"$missing_exporter_stderr")"
[[ "$missing_exporter_error" == *"otlphttp/tempo"* ]] || \
  fail "Missing exporter error did not name otlphttp/tempo"
[[ "$missing_exporter_error" == *"otelCollector.extraExporters"* ]] || \
  fail "Missing exporter error did not name otelCollector.extraExporters"

python3 - "$(manifest_for "$TMP/default")" "$(manifest_for "$TMP/port-3001")" "$(manifest_for "$TMP/auth-header")" "$(manifest_for "$TMP/tempo")" <<'PY'
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
tempo_config, tempo_checksum = config_and_checksum(sys.argv[4])

default_parsed = yaml.safe_load(default_config)
tempo_parsed = yaml.safe_load(tempo_config)
default_exporters = default_parsed["exporters"]
default_trace_exporters = default_parsed["service"]["pipelines"]["traces"]["exporters"]
tempo_exporters = tempo_parsed["exporters"]
tempo_trace_exporters = tempo_parsed["service"]["pipelines"]["traces"]["exporters"]

assert set(default_exporters) == {"otlphttp/langfuse", "debug"}, (
    "Default collector exporters changed or unexpectedly include Tempo"
)
assert default_trace_exporters == ["otlphttp/langfuse", "debug"], (
    "Default traces pipeline exporters changed or unexpectedly include Tempo"
)
assert tempo_exporters.get("otlphttp/tempo") == {"endpoint": "http://tempo:4318"}, (
    "Tempo exporter configuration was not rendered exactly"
)
assert tempo_trace_exporters == ["otlphttp/langfuse", "debug", "otlphttp/tempo"], (
    "Tempo exporter was not appended to the traces pipeline"
)
assert default_config != tempo_config, "Tempo values did not change the collector ConfigMap body"
assert default_checksum != tempo_checksum, (
    "Tempo values changed but Deployment checksum/config stayed identical. The pod would not roll."
)

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
