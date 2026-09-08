#!/usr/bin/env bash
# Cluster-tier proof for #2296: live database revision 0039 against a
# status-eligible v0.8.4 Helm target is refused before Helm mutates, and the
# API Deployment stays one ready replica. Disposable kind cluster; fake Helm
# history supplies the 0.8.4/0.8.5 revisions, real kubectl execs into a stub
# API pod whose `alembic current` prints 0039.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLUSTER="${CURIE_2296_CLUSTER:-curie-2296}"
NS="${CURIE_2296_NAMESPACE:-agent-ns}"
RELEASE="${CURIE_2296_RELEASE:-prod-release}"
CURIE_BIN="${CURIE_BIN:-}"

if [[ -z "$CURIE_BIN" ]]; then
  for candidate in \
    "$ROOT/cli/target/debug/curie" \
    "$ROOT/cli/target/release/curie" \
    "${CARGO_TARGET_DIR:-}/debug/curie" \
    "$HOME/.cargo/shared-targets/agentos/debug/curie"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      CURIE_BIN="$candidate"
      break
    fi
  done
  if [[ -z "$CURIE_BIN" ]]; then
    echo "build the CLI first (cargo build -p curie) or set CURIE_BIN" >&2
    exit 1
  fi
fi

PREV_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
cleanup() {
  if [[ -n "${PREV_CONTEXT:-}" ]]; then
    kubectl config use-context "$PREV_CONTEXT" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP_CLUSTER:-}" == "1" ]]; then
    return
  fi
  kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-${CLUSTER}"

kubectl create namespace "$NS"
cat <<'YAML' | kubectl apply -n "$NS" -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: alembic-stub
data:
  alembic: |
    #!/bin/sh
    echo "0039 (head)"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prod-release-curie-api
  labels:
    app.kubernetes.io/component: api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: stub-api
  template:
    metadata:
      labels:
        app: stub-api
        app.kubernetes.io/component: api
    spec:
      containers:
        - name: api
          image: busybox:1.36
          command: ["sleep", "3600"]
          volumeMounts:
            - name: stubs
              mountPath: /stubs
      volumes:
        - name: stubs
          configMap:
            name: alembic-stub
            defaultMode: 0755
YAML

kubectl -n "$NS" rollout status deploy/prod-release-curie-api --timeout=90s
POD="$(kubectl -n "$NS" get pod -l app=stub-api -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$NS" exec "$POD" -c api -- sh -c 'cp /stubs/alembic /bin/alembic && chmod +x /bin/alembic'
kubectl -n "$NS" exec "$POD" -c api -- alembic -c alembic.ini current | grep -q 0039

WORKDIR="$(mktemp -d)"
cat >"$WORKDIR/history.json" <<'JSON'
[
  {"revision":1,"status":"superseded","chart":"curie-0.8.4","app_version":"0.8.4","description":"Upgrade complete"},
  {"revision":2,"status":"failed","chart":"curie-0.8.5","app_version":"0.8.5","description":"RuntimeClass \"gvisor\" not found"},
  {"revision":3,"status":"deployed","chart":"curie-0.8.5","app_version":"0.8.5","description":"Upgrade complete"}
]
JSON
cat >"$WORKDIR/helm" <<EOF
#!/bin/sh
echo "\$*" >> "$WORKDIR/helm-argv.log"
case "\$1" in
history) cat "$WORKDIR/history.json" ;;
rollback) echo rollback-ran >> "$WORKDIR/helm-argv.log"; echo 'Rollback was a success.' ;;
*) echo "unexpected helm verb: \$1" >&2; exit 1 ;;
esac
EOF
chmod +x "$WORKDIR/helm"

BEFORE="$(kubectl -n "$NS" get deploy prod-release-curie-api -o jsonpath='{.status.replicas}/{.status.readyReplicas}/{.status.unavailableReplicas}')"
PATH="$WORKDIR:$(dirname "$(command -v kubectl)"):$PATH" \
  "$CURIE_BIN" cluster rollback --namespace "$NS" --release "$RELEASE" --yes --json \
  >"$WORKDIR/out.json" 2>"$WORKDIR/err.txt" || true

python3 - <<PY
import json, pathlib, sys
out = pathlib.Path("$WORKDIR/out.json").read_text()
err = pathlib.Path("$WORKDIR/err.txt").read_text()
print("stdout:", out)
print("stderr:", err)
print("helm log:", pathlib.Path("$WORKDIR/helm-argv.log").read_text() if pathlib.Path("$WORKDIR/helm-argv.log").exists() else "<missing>")
payload = json.loads(out.strip().splitlines()[-1])
error = payload.get("error") or ""
fix = payload.get("fix") or ""
assert payload.get("rolled_back") is not True, payload
assert "0039" in error and "0.8.4" in error, payload
assert "0.8.5" in fix, payload
assert "secret" not in error.lower() and "postgresql://" not in error
print("refusal:", error)
print("fix:", fix)
PY

if grep -q rollback-ran "$WORKDIR/helm-argv.log"; then
  echo "helm rollback was invoked" >&2
  cat "$WORKDIR/helm-argv.log" >&2
  exit 1
fi

AFTER="$(kubectl -n "$NS" get deploy prod-release-curie-api -o jsonpath='{.status.replicas}/{.status.readyReplicas}/{.status.unavailableReplicas}')"
echo "replicas before=$BEFORE after=$AFTER"
python3 - <<PY
before = "$BEFORE".split("/")
after = "$AFTER".split("/")
# replicas / readyReplicas / unavailableReplicas (empty unavailable -> 0)
def n(v):
    return int(v) if v else 0
assert n(before[0]) == 1 and n(after[0]) == 1, (before, after)
assert n(before[1]) == 1 and n(after[1]) == 1, (before, after)
assert n(before[2]) == 0 and n(after[2]) == 0, (before, after)
print("one coherent serving replica; no surplus or unavailable API replica")
PY

echo "e2e-cluster-rollback-schema: pass"
