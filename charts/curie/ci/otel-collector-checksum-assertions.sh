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
render service-wiring \
  --set dispatcher.slack.appToken=test-app-token \
  --set dispatcher.slack.botToken=test-bot-token \
  --set dispatcher.slack.signingSecret=test-signing-secret
render no-collector \
  --set otelCollector.deploy=false \
  --set dispatcher.slack.appToken=test-app-token \
  --set dispatcher.slack.botToken=test-bot-token \
  --set dispatcher.slack.signingSecret=test-signing-secret

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

python3 - \
  "$(manifest_for "$TMP/default")" \
  "$(manifest_for "$TMP/port-3001")" \
  "$(manifest_for "$TMP/auth-header")" \
  "$(manifest_for "$TMP/tempo")" \
  "$TMP/service-wiring" \
  "$TMP/no-collector" <<'PY'
from pathlib import Path
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


def all_docs(root):
    return [
        doc
        for path in Path(root).rglob("*.yaml")
        for doc in yaml.safe_load_all(path.read_text())
        if doc
    ]


def container_env(docs, component):
    if component == "runner":
        workload = next(
            doc
            for doc in docs
            if doc.get("kind") == "SandboxTemplate"
        )
        pod_spec = workload["spec"]["podTemplate"]["spec"]
    else:
        workload = next(
            doc
            for doc in docs
            if doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("labels", {}).get(
                "app.kubernetes.io/component"
            )
            == component
        )
        pod_spec = workload["spec"]["template"]["spec"]
    container = next(item for item in pod_spec["containers"] if item["name"] == component)
    return {item["name"]: item for item in container.get("env", [])}


wired_docs = all_docs(sys.argv[5])
no_collector_docs = all_docs(sys.argv[6])
endpoint = "http://curie-otel-collector:4318"
for component in ("api", "dispatcher", "worker", "runner"):
    env = container_env(wired_docs, component)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == {
        "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
        "value": endpoint,
    }, f"{component}: standard OTLP endpoint is missing or not the in-cluster collector"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == {
        "name": "OTEL_EXPORTER_OTLP_PROTOCOL",
        "value": "http/protobuf",
    }, f"{component}: standard OTLP HTTP protocol is missing"
    assert "OTEL_SERVICE_NAME" not in env, (
        f"{component}: service.name belongs to configure(service_name=...), not chart env"
    )
    assert "CURIE_RUNNER_OTEL_EXPORTER_OTLP_ENDPOINT" not in env, (
        f"{component}: Helm has one in-cluster endpoint; the Compose relay is not a chart contract"
    )

    disabled_env = container_env(no_collector_docs, component)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in disabled_env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in disabled_env

default_parsed = yaml.safe_load(default_config)
tempo_parsed = yaml.safe_load(tempo_config)
default_exporters = default_parsed["exporters"]
default_trace_exporters = default_parsed["service"]["pipelines"]["traces"]["exporters"]
default_log_pipeline = default_parsed["service"]["pipelines"]["logs"]
tempo_exporters = tempo_parsed["exporters"]
tempo_trace_exporters = tempo_parsed["service"]["pipelines"]["traces"]["exporters"]

assert set(default_exporters) == {"otlphttp/langfuse", "debug"}, (
    "Default collector exporters changed or unexpectedly include Tempo"
)
assert default_trace_exporters == ["otlphttp/langfuse", "debug"], (
    "Default traces pipeline exporters changed or unexpectedly include Tempo"
)
assert default_log_pipeline == {
    "receivers": ["otlp"],
    "processors": ["batch"],
    "exporters": ["debug"],
}, "The chart collector must accept batched OTLP logs without claiming a retained backend"
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

print(
    "ConfigMap rollout, OTLP logs pipeline, and four-service standard env wiring: OK"
)
PY
