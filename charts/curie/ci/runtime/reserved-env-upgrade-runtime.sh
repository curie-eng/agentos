#!/usr/bin/env bash
# Released v0.8.4 retained-values upgrade refuses duplicates before hooks/mutation.
# Run only on an owned disposable kind cluster; no product images are started.
set -euo pipefail
: "${KUBECONFIG:?set a private kubeconfig for an owned disposable kind cluster}"
context="$(kubectl config current-context)"
[[ "$context" == kind-* ]] || { echo "requires an owned kind context" >&2; exit 2; }
CHART="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
namespace="acme-reserved-env-${RANDOM}"
created=false
cleanup() {
  if [[ "$created" == true ]]; then
    kubectl --context "$context" delete namespace "$namespace" --wait=true --timeout=90s
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
curl -fsSL --retry 2 https://github.com/curie-eng/curie/releases/download/v0.8.4/curie-0.8.4.tgz -o "$TMP/released.tgz"
echo "fee20ab73c05d7a888165f980fb82d25150fcba509f19218bafc4c187a9044bb  $TMP/released.tgz" | sha256sum -c -
[[ "$(helm show chart "$TMP/released.tgz" | awk '$1 == "version:" {print $2}')" == 0.8.4 ]]
# The published v0.8.4 chart already has the typed timeout; its initial install
# accepts the duplicate. Keep that observed release behavior in this fixture.
# Zero replicas isolates Helm upgrade semantics from provider credentials or images.
cat > "$TMP/legacy.yaml" <<'YAML'
security:
  allowDevDefaults: true
  checkDefaultCredentials: false
  gvisor:
    enabled: false
  networkPolicy:
    enabled: false
preflights:
  avxCheck:
    enabled: false
  networkPolicyProbe:
    enabled: false
  controllerReady:
    enabled: false
postgres:
  deploy: false
  host: postgres.example.com
valkey:
  deploy: false
  host: valkey.example.com
clickhouse:
  deploy: false
  host: clickhouse.example.com
rustfs:
  deploy: false
  host: object-store.example.com
  egress:
    - cidr: 192.0.2.0/24
      port: 443
langfuse:
  deploy: false
  host: traces.example.com
otelCollector:
  deploy: false
  telemetryDisabled: true
agentSandbox:
  deploy: false
  controller:
    deploy: false
api:
  replicas: 0
ui:
  deploy: false
dispatcher:
  deploy: false
worker:
  replicas: 0
  publication:
    enabled: false
  upgradeDrain:
    enabled: false
  extraEnv:
    - name: CURIE_RUNNER_TOTAL_TIMEOUT_S
      value: "600"
YAML
kubectl --context "$context" create namespace "$namespace"
created=true
helm install acme "$TMP/released.tgz" --kube-context "$context" -n "$namespace" -f "$TMP/legacy.yaml" --skip-crds --no-hooks
helm get values acme --kube-context "$context" -n "$namespace" -o yaml > "$TMP/retained.yaml"
helm get manifest acme --kube-context "$context" -n "$namespace" > "$TMP/old-manifest.yaml"
python3 - "$TMP/old-manifest.yaml" <<'PY'
import sys,yaml
worker=next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get('kind')=='Deployment' and d['metadata']['name']=='acme-curie-worker')
env=worker['spec']['template']['spec']['containers'][0]['env']
assert [e for e in env if e['name']=='CURIE_RUNNER_TOTAL_TIMEOUT_S']==[{'name':'CURIE_RUNNER_TOTAL_TIMEOUT_S','value':'600'}]*2
print('OK: released v0.8.4 installed with two timeout entries and retained the legacy extraEnv value')
PY
# Let the deployment controllers observe the zero-replica fixture before snapshots.
kubectl --context "$context" rollout status deployment/acme-curie-worker -n "$namespace" --timeout=60s
kubectl --context "$context" rollout status deployment/acme-curie-api -n "$namespace" --timeout=60s
snapshot() {
  kubectl --context "$context" get deployment,service,secret,configmap,serviceaccount,role,rolebinding,job -n "$namespace" -o json |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(sorted(d["items"], key=lambda x:(x["kind"],x["metadata"]["name"])),sort_keys=True))'
}
snapshot > "$TMP/before.json"
helm history acme --kube-context "$context" -n "$namespace" -o json > "$TMP/history-before.json"
for timeout in 600 599; do
  # The enabled drain is a real pre-upgrade hook. A changed annotation would
  # mutate both deployments if Helm reached its apply phase.
  if helm upgrade acme "$CHART" --kube-context "$context" -n "$namespace" \
      -f "$TMP/retained.yaml" --set worker.runnerTotalTimeoutSeconds="$timeout" \
      --set worker.upgradeDrain.enabled=true --set-string placement.platform.annotations.upgrade-probe=changed \
      > "$TMP/refusal.log" 2>&1; then
    echo "accepted legacy duplicate on upgrade" >&2; exit 1
  fi
  cat "$TMP/refusal.log"
  grep -Eq 'worker.extraEnv contains chart-owned environment variable CURIE_RUNNER_TOTAL_TIMEOUT_S.*worker.runnerTotalTimeoutSeconds' "$TMP/refusal.log"
  snapshot > "$TMP/after.json"
  cmp "$TMP/before.json" "$TMP/after.json"
  helm history acme --kube-context "$context" -n "$namespace" -o json > "$TMP/history-after.json"
  cmp "$TMP/history-before.json" "$TMP/history-after.json"
  [[ "$(kubectl --context "$context" get jobs -n "$namespace" -o jsonpath='{.items}')" == '[]' ]]
  echo "OK: timeout=$timeout refused; release revision and all observed resources unchanged; no pre-upgrade job"
done
# Remove only the guard in an isolated chart copy and replay the retained values.
cp -a "$CHART" "$TMP/mutant"
python3 - "$TMP/mutant/templates/_reserved-env.tpl" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]); s=p.read_text(); old='{{- if hasKey $reserved $name -}}'; assert s.count(old)==1;p.write_text(s.replace(old,'{{- if false -}}'))
PY
helm template acme "$TMP/mutant" -f "$TMP/retained.yaml" --is-upgrade --output-dir "$TMP/control" >/dev/null
python3 - "$TMP/control/curie/templates/worker.yaml" <<'PY'
import sys,yaml
worker=next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get('kind')=='Deployment')
env=worker['spec']['template']['spec']['containers'][0]['env']
assert len([e for e in env if e['name']=='CURIE_RUNNER_TOTAL_TIMEOUT_S'])==2
print('OK: removing only reserved-name guard reproduces two timeout entries from released retained values')
PY
