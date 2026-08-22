#!/usr/bin/env bash
#
# chart-runtime-e2e.sh -- one-command RUNTIME e2e for the Curie Helm chart.
#
# WHY THIS EXISTS
# ---------------
# `helm lint` and `helm template` render manifests but NEVER run a container.
# They cannot catch a bug that only manifests when an init container executes --
# for example issue #56, where the bundle fetch client wrote the S3 credential
# in cleartext to a config file,
# and that dir sat on the `bundles` emptyDir the untrusted `runner` container
# also mounts. The acceptance criterion is a RUNTIME exec:
#   kubectl exec <sandbox> -c runner -- find /bundles -name config.json   # empty
# This harness makes that check and the API Service NodePort contract one
# command.
#
# THE PATTERN (reusable -- this is NOT a #56 one-off)
# ---------------------------------------------------
# 1. Install a trimmed chart slice on k8scratch with RustFS and agent sandbox.
# 2. Assert the API Service pinned and auto allocated NodePort cases.
# 3. Seed a real bundle object into RustFS.
# 4. Render the SandboxTemplate.spec.podTemplate into a BOUND sandbox Pod
#    (CURIE_BUNDLE_REF pointing at the seeded object) and apply it -- so the
#    real bundle-fetch/bundle-extract init containers actually run.
# 5. Wait for the init pair to complete and the runner container to be Running.
# 6. `kubectl exec` into the runner and ASSERT on what it can see.
#
# HOW TO ADD ANOTHER RUNTIME ASSERTION
# ------------------------------------
# The generic seam is "render template -> bind bundle -> exec runner -> assert".
# To add a new runtime check, add a new `kubectl exec e2e-bound-sandbox -c runner
# -- <cmd>` inside run_assertions() and fold its result into the PASS/FAIL logic.
# Reuse exec_echo() so the command and its raw output are auditable in the log.
#
set -euo pipefail

# --------------------------------------------------------------------------
# Config / flags
# --------------------------------------------------------------------------
NAMESPACE="curie-e2eharness"
RELEASE="e2eharness"
CHART="charts/curie"
KEEP=0
FORCE=0
RUNNER_IMAGE="${CURIE_CHART_E2E_RUNNER_IMAGE:-}"
EXPECT_VULNERABLE=0
POD_NAME="e2e-bound-sandbox"
BUNDLE_REF="e2e/probe.tgz"

usage() {
  cat <<'EOF'
Usage: scripts/chart-runtime-e2e.sh [options]

Stands up a trimmed Curie chart slice on the k8scratch cluster, proves the API
Service pinned and auto allocated NodePort cases, seeds a real bundle into
RustFS, renders a bound agent-sandbox Pod, runs its bundle-fetch/extract init
containers, and execs the runner to assert the #56 credential is NOT readable
off the shared bundle volume (and the bundle really was provisioned).

Options:
  --namespace <ns>       Namespace to use (default: curie-e2eharness)
  --release <name>       Helm release name (default: e2eharness)
  --chart <path>         Chart path, relative to repo root (default: charts/curie)
  --runner-image <img>   Override ONLY the runner container image with this image
                         (command: sleep 3600, probes/ports stripped). Robustness
                         fallback when the real runner image will not reach Running
                         on the cluster; the #56 assertion only needs the runner's
                         VIEW of the shared bundle mount. Default: real runner image.
  --expect-vulnerable    Negative-control mode: INVERT the security assertion, so
                         PASS means the credential IS exposed. Point at an unfixed
                         template to prove the harness discriminates.
  --keep                 Skip teardown (leave namespace + release for debugging).
  --force                Allow running against a non-k8scratch kube context.
  --help                 Show this help.

Environment:
  CURIE_CHART_E2E_RUNNER_IMAGE
                         Runner image fallback for callers that cannot forward
                         flags. --runner-image takes precedence when supplied.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --chart) CHART="$2"; shift 2 ;;
    --runner-image) RUNNER_IMAGE="$2"; shift 2 ;;
    --expect-vulnerable) EXPECT_VULNERABLE=1; shift ;;
    --keep) KEEP=1; shift ;;
    --force) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Resolve repo root from this script's location so --chart is repo-relative.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
case "$CHART" in
  /*) CHART_PATH="$CHART" ;;
  *) CHART_PATH="$REPO_ROOT/$CHART" ;;
esac

# Derived resource names -- mirror the chart's `curie.fullname` helper exactly
# (nameOverride/fullnameOverride empty): fullname == release if it already
# contains the chart name "curie", else "<release>-curie".
if [[ "$RELEASE" == *curie* ]]; then
  FULLNAME="$RELEASE"
else
  FULLNAME="$RELEASE-curie"
fi
SANDBOX_TEMPLATE="$FULLNAME-runner"
RUSTFS_SVC="$FULLNAME-rustfs"
API_SERVICE="$FULLNAME-api"
PLATFORM_PRIORITY_CLASS="$FULLNAME-platform"
SANDBOX_PRIORITY_CLASS="$FULLNAME-sandbox"
SECRET_NAME="$FULLNAME-secrets"
RUSTFS_BUCKET="curie-bundles"
RUSTFS_ACCESS_KEY="rustfs"
# Ownership label stamped on any namespace THIS script creates. The script only
# ever deletes a namespace carrying this label, so pointing --namespace at a
# pre-existing namespace (e.g. `default`) can never destroy it.
OWNED_LABEL="curie-e2e-harness/owned"

banner() { echo; echo "== $* =="; }
fail() { echo; echo "FAIL: $*"; exit 1; }

# ns_is_owned <ns> : true iff the namespace exists AND carries the ownership label.
ns_is_owned() {
  local ns="$1" val
  val="$(kubectl get ns "$ns" -o "jsonpath={.metadata.labels['curie-e2e-harness/owned']}" 2>/dev/null || echo "")"
  [[ "$val" == "true" ]]
}

# create_owned_ns <ns> : create the namespace and stamp the ownership label on it.
create_owned_ns() {
  local ns="$1"
  kubectl create ns "$ns"
  kubectl label ns "$ns" "${OWNED_LABEL}=true" --overwrite >/dev/null
}

# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------
CURRENT_CTX="$(kubectl config current-context 2>/dev/null || echo "")"
if [[ "$CURRENT_CTX" != "k8scratch" && "$FORCE" -ne 1 ]]; then
  fail "kube context is '$CURRENT_CTX', not 'k8scratch'. Refusing (override with --force)."
fi

# --------------------------------------------------------------------------
# Teardown (trap on EXIT). Leaves the cluster-scoped sandbox* CRDs alone.
# --------------------------------------------------------------------------
teardown() {
  local rc=$?
  if [[ "$KEEP" -eq 1 ]]; then
    banner "TEARDOWN skipped (--keep); namespace $NAMESPACE left in place"
    return $rc
  fi
  banner "TEARDOWN"
  helm uninstall "$RELEASE" -n "$NAMESPACE" --no-hooks >/dev/null 2>&1 || true
  kubectl delete priorityclass \
    "$PLATFORM_PRIORITY_CLASS" "$SANDBOX_PRIORITY_CLASS" \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  # Only ever delete a namespace THIS script created (carries the ownership label).
  if ns_is_owned "$NAMESPACE"; then
    kubectl delete ns "$NAMESPACE" --wait=false >/dev/null 2>&1 || true
  else
    echo "namespace $NAMESPACE not owned by this harness; leaving it in place"
  fi
  return $rc
}
trap teardown EXIT

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# exec_echo <label> <exec-args...> : run a kubectl exec, echo the command, its
# exit code, and its stdout/stderr SEPARATELY. Captures stdout into the global
# EXEC_OUT, stderr into EXEC_ERR, and the exit code into EXEC_RC for the caller.
# Verdicts MUST key on EXEC_OUT (stdout) only -- folding stderr into the captured
# value can make an empty result look non-empty (e.g. a "Permission denied" line)
# and yield a false PASS.
exec_echo() {
  local label="$1"; shift
  echo "--- $label"
  echo "\$ kubectl exec $POD_NAME -n $NAMESPACE $*"
  local out_file err_file
  out_file="$(mktemp /tmp/e2e-exec-out.XXXXXX)"
  err_file="$(mktemp /tmp/e2e-exec-err.XXXXXX)"
  set +e
  kubectl exec "$POD_NAME" -n "$NAMESPACE" "$@" >"$out_file" 2>"$err_file"
  EXEC_RC=$?
  set -e
  EXEC_OUT="$(cat "$out_file")"
  EXEC_ERR="$(cat "$err_file")"
  rm -f "$out_file" "$err_file"
  echo "[exit $EXEC_RC] stdout:"
  printf '%s\n' "$EXEC_OUT" | sed 's/^/    /'
  if [[ -n "$EXEC_ERR" ]]; then
    echo "stderr:"
    printf '%s\n' "$EXEC_ERR" | sed 's/^/    /'
  fi
}

# build_bound_pod : read the installed SandboxTemplate, extract its podTemplate,
# and emit a bound `kind: Pod` manifest on stdout. This is the generic
# "render template -> bind bundle" step; any runtime assertion reuses the Pod.
build_bound_pod() {
  local template_json="$1"
  POD_NAME="$POD_NAME" NAMESPACE="$NAMESPACE" BUNDLE_REF="$BUNDLE_REF" \
    RUNNER_IMAGE="$RUNNER_IMAGE" python3 - "$template_json" <<'PY'
import json, os, sys

st = json.load(open(sys.argv[1]))
pod_tmpl = st["spec"]["podTemplate"]
spec = dict(pod_tmpl.get("spec", {}))

pod_name = os.environ["POD_NAME"]
namespace = os.environ["NAMESPACE"]
bundle_ref = os.environ["BUNDLE_REF"]
runner_image = os.environ.get("RUNNER_IMAGE", "")

# Bind the bundle: replace any CURIE_BUNDLE_REF env (drop valueFrom) with the
# seeded object key, in every container and initContainer that declares it.
def bind_ref(container):
    env = container.get("env")
    if not env:
        return
    for e in env:
        if e.get("name") == "CURIE_BUNDLE_REF":
            e.clear()
            e["name"] = "CURIE_BUNDLE_REF"
            e["value"] = bundle_ref

for c in spec.get("initContainers", []):
    bind_ref(c)
for c in spec.get("containers", []):
    bind_ref(c)

# Use the default ServiceAccount (avoids a missing-SA scheduling failure).
spec.pop("serviceAccountName", None)
spec.pop("automountServiceAccountToken", None)
spec["restartPolicy"] = "Never"

# Optional runner-image override: swap ONLY the runner container's image for a
# trivially-runnable one. The #56 assertion depends on the runner's VIEW of the
# shared bundle mount, not the runner binary, so this is a safe fallback when the
# real runner image will not reach Running on the cluster.
if runner_image:
    for c in spec.get("containers", []):
        if c.get("name") == "runner":
            c["image"] = runner_image
            c["command"] = ["/bin/sh", "-c", "sleep 3600"]
            c.pop("readinessProbe", None)
            c.pop("livenessProbe", None)
            c.pop("ports", None)

pod = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": pod_name,
        "namespace": namespace,
        "labels": pod_tmpl.get("metadata", {}).get("labels", {}),
    },
    "spec": spec,
}
print(json.dumps(pod))
PY
}

# wait_for_init_complete : block until BOTH init containers are terminated with
# reason=Completed and the runner container is state=running (not necessarily
# Ready -- /healthz readiness may never flip, but exec works on a Running
# container). Dumps diagnostics and fails on timeout.
wait_for_init_complete() {
  local timeout=180 waited=0 interval=4
  while true; do
    local pod_json
    pod_json="$(kubectl get pod "$POD_NAME" -n "$NAMESPACE" -o json 2>/dev/null || echo '{}')"
    local ready
    ready="$(printf '%s' "$pod_json" | python3 -c '
import json, sys
p = json.load(sys.stdin)
st = p.get("status", {})
inits = st.get("initContainerStatuses", [])
conts = st.get("containerStatuses", [])
init_ok = len(inits) >= 2 and all(
    (c.get("state", {}).get("terminated", {}) or {}).get("reason") == "Completed"
    for c in inits
)
runner_running = any(
    c.get("name") == "runner" and "running" in c.get("state", {})
    for c in conts
)
print("yes" if (init_ok and runner_running) else "no")
' 2>/dev/null || echo "no")"
    if [[ "$ready" == "yes" ]]; then
      return 0
    fi
    if [[ "$waited" -ge "$timeout" ]]; then
      banner "DIAGNOSTICS (timeout after ${timeout}s)"
      kubectl describe pod "$POD_NAME" -n "$NAMESPACE" || true
      echo "--- bundle-fetch logs"
      kubectl logs "$POD_NAME" -n "$NAMESPACE" -c bundle-fetch || true
      echo "--- bundle-extract logs"
      kubectl logs "$POD_NAME" -n "$NAMESPACE" -c bundle-extract || true
      fail "bound sandbox did not reach (init Completed + runner Running) within ${timeout}s"
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
}

# run_assertions : exec the runner and evaluate the positive control + the #56
# security assertion. Sets global RESULT to "PASS" or "FAIL".
run_assertions() {
  RESULT="FAIL"

  # POSITIVE CONTROL: prove the bundle was actually fetched + extracted, so an
  # empty config.json result below is meaningful (not a no-op fetch).
  exec_echo "positive control: bundle manifest present" \
    -c runner -- sh -c 'find /bundles/current -name plugin.json'
  # Bundle present only if the exec succeeded AND the manifest path printed on
  # STDOUT (stderr like "Permission denied" must never count as present).
  if [[ "$EXEC_RC" -ne 0 || -z "${EXEC_OUT//[[:space:]]/}" ]]; then
    banner "DIAGNOSTICS (positive control failed)"
    kubectl logs "$POD_NAME" -n "$NAMESPACE" -c bundle-fetch || true
    kubectl logs "$POD_NAME" -n "$NAMESPACE" -c bundle-extract || true
    fail "bundle not provisioned (no plugin.json under /bundles/current); test inconclusive"
  fi

  # SECURITY ASSERTION (#56): the RustFS credential must not be readable off the
  # shared bundle volume from the runner's view.
  exec_echo "security: S3 client credential files on shared volume" \
    -c runner -- sh -c "find /bundles \\( -name config.json -o -name credentials \\)"
  local config_hits="$EXEC_OUT"
  exec_echo "security: persisted AWS credential fields on shared volume" \
    -c runner -- sh -c "grep -rEl 'aws_(access_key_id|secret_access_key)' /bundles 2>/dev/null || true"
  local cred_hits="$EXEC_OUT"

  local exposed=0
  if [[ -n "${config_hits//[[:space:]]/}" || -n "${cred_hits//[[:space:]]/}" ]]; then
    exposed=1
  fi

  echo
  if [[ "$EXPECT_VULNERABLE" -eq 1 ]]; then
    # Negative control: the credential SHOULD be exposed on an unfixed template.
    if [[ "$exposed" -eq 1 ]]; then
      echo "negative control: credential IS exposed on shared volume (expected on unfixed template)"
      RESULT="PASS"
    else
      echo "negative control: credential NOT exposed, but --expect-vulnerable expected it"
      RESULT="FAIL"
    fi
  else
    if [[ "$exposed" -eq 0 ]]; then
      echo "security assertion clean: no config.json and no cleartext credential on shared volume"
      RESULT="PASS"
    else
      echo "security assertion FAILED: RustFS credential is readable off the shared bundle volume"
      RESULT="FAIL"
    fi
  fi
}

# --------------------------------------------------------------------------
# 1. Fresh namespace + trimmed install
# --------------------------------------------------------------------------
banner "PRECHECK context=$CURRENT_CTX namespace=$NAMESPACE release=$RELEASE"

if kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  # Only reclaim a namespace WE created. An unlabeled pre-existing namespace
  # (e.g. `default`, or someone else's) must never be deleted by this harness.
  if ns_is_owned "$NAMESPACE"; then
    banner "namespace $NAMESPACE already exists (harness-owned) -- cleaning up before run"
    helm uninstall "$RELEASE" -n "$NAMESPACE" --no-hooks >/dev/null 2>&1 || true
    kubectl delete priorityclass \
      "$PLATFORM_PRIORITY_CLASS" "$SANDBOX_PRIORITY_CLASS" \
      --ignore-not-found --wait=true >/dev/null 2>&1 || true
    kubectl delete ns "$NAMESPACE" --wait=true --timeout=120s >/dev/null 2>&1 || true
  else
    fail "namespace $NAMESPACE already exists and is NOT owned by this harness (missing label ${OWNED_LABEL}=true). Remove it manually or pick another --namespace."
  fi
fi

# Pre-create the namespace stamped with the ownership label so teardown/cleanup
# can only ever delete a namespace this script created.
create_owned_ns "$NAMESPACE"

banner "INSTALL trimmed chart"
CHART_VALUES=(
  -f "$CHART_PATH/values-e2e-nogvisor.yaml"
  -f "$CHART_PATH/values-e2e-harness.yaml"
  --set api.deploy=true
  --set api.replicas=0
  --set api.service.type=NodePort
  # The shared scratch cluster may already allocate the chart's public default.
  --set api.service.nodePort=30181
  # PriorityClasses are cluster scoped. Give this release unique names and let
  # the chart create them so the harness never borrows another release's state.
  --set priorityClasses.platform.create=true
  --set-string priorityClasses.platform.name="$PLATFORM_PRIORITY_CLASS"
  --set priorityClasses.sandbox.create=true
  --set-string priorityClasses.sandbox.name="$SANDBOX_PRIORITY_CLASS"
  # The trimmed overlay disables these backing stores. The zero-replica API
  # still renders their environment, so satisfy the chart's BYO-host guards
  # without creating reachable dependencies.
  --set-string postgres.host=postgres.example.com
  --set-string valkey.host=valkey.example.com
)
install_chart() {
  helm install "$RELEASE" "$CHART_PATH" \
    -n "$NAMESPACE" --no-hooks \
    "${CHART_VALUES[@]}"
}
# k8scratch is shared and slightly flaky: a spurious "namespaces not found" at
# install is usually API churn, so retry ONCE before failing.
if ! install_chart; then
  echo "helm install failed once (likely transient API churn on shared node); retrying in 5s..."
  sleep 5
  kubectl delete priorityclass \
    "$PLATFORM_PRIORITY_CLASS" "$SANDBOX_PRIORITY_CLASS" \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  # Recreate the labeled namespace before retrying; only ever delete our own.
  if ns_is_owned "$NAMESPACE"; then
    kubectl delete ns "$NAMESPACE" --wait=true --timeout=60s >/dev/null 2>&1 || true
  fi
  create_owned_ns "$NAMESPACE"
  install_chart || fail "helm install failed twice"
fi

# --------------------------------------------------------------------------
# 2. Assert the API Service pinned NodePort, then restore auto allocation.
# --------------------------------------------------------------------------
banner "ASSERT API Service pinned NodePort"
API_SERVICE_JSON="$(kubectl get service "$API_SERVICE" -n "$NAMESPACE" -o json)"
python3 - "$API_SERVICE_JSON" <<'PY' || exit 1
import json
import sys

service = json.loads(sys.argv[1])
service_type = service.get("spec", {}).get("type")
ports = [port for port in service.get("spec", {}).get("ports", []) if port.get("name") == "http"]
if service_type != "NodePort":
    print(f"FAIL: live API Service type {service_type!r}, expected 'NodePort'", file=sys.stderr)
    sys.exit(1)
if len(ports) != 1:
    print(f"FAIL: live API Service has {len(ports)} http ports, expected 1", file=sys.stderr)
    sys.exit(1)
node_port = ports[0].get("nodePort")
if node_port != 30181:
    print(f"FAIL: live API Service nodePort {node_port!r}, expected 30181", file=sys.stderr)
    sys.exit(1)
print("live API Service: type=NodePort nodePort=30181")
PY

banner "UPGRADE API Service to auto allocated NodePort"
helm upgrade "$RELEASE" "$CHART_PATH" \
  -n "$NAMESPACE" --no-hooks \
  "${CHART_VALUES[@]}" \
  --set-string api.service.nodePort=

STORED_VALUES_JSON="$(helm get values "$RELEASE" -n "$NAMESPACE" -o json)"
python3 - "$STORED_VALUES_JSON" <<'PY' || exit 1
import json
import sys

values = json.loads(sys.argv[1])
service = values.get("api", {}).get("service", {})
if "nodePort" not in service or service["nodePort"] != "":
    print(
        f"FAIL: stored api.service.nodePort is {service.get('nodePort')!r}, expected an explicit empty string",
        file=sys.stderr,
    )
    sys.exit(1)
print('stored override: api.service.nodePort=""')
PY

API_SERVICE_MANIFEST="$(helm get manifest "$RELEASE" -n "$NAMESPACE" | awk -v name="$API_SERVICE" '
  BEGIN { RS = "---" }
  {
    is_service = 0
    has_name = 0
    line_count = split($0, lines, "\n")
    for (i = 1; i <= line_count; i++) {
      line = lines[i]
      if (line ~ /^[[:space:]]*kind:[[:space:]]*Service[[:space:]]*$/) {
        is_service = 1
      }
      candidate = line
      if (sub(/^[[:space:]]*name:[[:space:]]*/, "", candidate)) {
        sub(/[[:space:]]*$/, "", candidate)
        if (candidate == name) {
          has_name = 1
        }
      }
    }
    if (is_service && has_name) {
      print
      found = 1
      exit
    }
  }
  END { if (!found) exit 1 }
')" || fail "API Service is absent from the stored release manifest"
if grep -q '^[[:space:]]*nodePort:' <<<"$API_SERVICE_MANIFEST"; then
  fail "stored release manifest renders nodePort after the empty override"
fi
echo "stored release manifest: API Service nodePort omitted"

kubectl delete service "$API_SERVICE" -n "$NAMESPACE" --wait=true
if kubectl get service "$API_SERVICE" -n "$NAMESPACE" >/dev/null 2>&1; then
  fail "API Service still exists after deletion"
fi
echo "live API Service deleted before auto allocation proof"

printf '%s\n' "$API_SERVICE_MANIFEST" | kubectl create -n "$NAMESPACE" -f -
API_SERVICE_JSON="$(kubectl get service "$API_SERVICE" -n "$NAMESPACE" -o json)"
python3 - "$API_SERVICE_JSON" <<'PY' || exit 1
import json
import sys

service = json.loads(sys.argv[1])
service_type = service.get("spec", {}).get("type")
ports = [port for port in service.get("spec", {}).get("ports", []) if port.get("name") == "http"]
if service_type != "NodePort":
    print(f"FAIL: live API Service type {service_type!r}, expected 'NodePort'", file=sys.stderr)
    sys.exit(1)
if len(ports) != 1:
    print(f"FAIL: live API Service has {len(ports)} http ports, expected 1", file=sys.stderr)
    sys.exit(1)
node_port = ports[0].get("nodePort")
if not isinstance(node_port, int) or not 30000 <= node_port <= 32767:
    print(
        f"FAIL: live auto allocated API Service nodePort {node_port!r} is outside 30000 through 32767",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"live API Service auto allocated nodePort={node_port}")
PY

# --------------------------------------------------------------------------
# 3. Wait for RustFS Running. Gate on the pod, not Helm release status.
# --------------------------------------------------------------------------
banner "WAIT RustFS Running"
if ! kubectl wait --for=condition=Ready pod \
    -l app.kubernetes.io/component=rustfs \
    -n "$NAMESPACE" --timeout=180s; then
  kubectl get pods -n "$NAMESPACE" || true
  kubectl describe pod -l app.kubernetes.io/component=rustfs -n "$NAMESPACE" || true
  fail "RustFS pod did not become Ready"
fi

# --------------------------------------------------------------------------
# 4. Seed a real bundle into RustFS
# --------------------------------------------------------------------------
banner "SEED bundle into RustFS"
# Build a VALID tar.gz (bundle-extract runs `set -eu; tar -xzf`, so a malformed
# archive fails the pod). Layout: myplugin/.claude-plugin/plugin.json.
SEED_DIR="$(mktemp -d /tmp/e2e-bundle.XXXXXX)"
mkdir -p "$SEED_DIR/myplugin/.claude-plugin"
cat > "$SEED_DIR/myplugin/.claude-plugin/plugin.json" <<'JSON'
{"name":"e2e-probe","version":"0.0.0"}
JSON
tar -czf "$SEED_DIR/probe.tgz" -C "$SEED_DIR" myplugin
# `base64 < file | tr -d '\n'` rather than GNU's `base64 -w0 file`: BSD base64
# (macOS) rejects both the `-w` flag and a bare filename operand -- it wants
# `-i file` -- so the previous `-w0` form fell through to a fallback that failed
# just as hard with `base64: invalid argument <path>`, blocking this script on a
# Mac at the seed step. Reading from stdin and stripping newlines here is the
# form both userlands accept, and `binaryData` below needs the single line.
PROBE_B64="$(base64 < "$SEED_DIR/probe.tgz" | tr -d '\n')"

# ConfigMap carrying the archive plus a one shot AWS CLI Job that creates the
# bucket and uploads the object using path addressing.
cat <<EOF | kubectl apply -n "$NAMESPACE" -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: e2e-bundle-seed
binaryData:
  probe.tgz: $PROBE_B64
---
apiVersion: batch/v1
kind: Job
metadata:
  name: e2e-bundle-seed
spec:
  backoffLimit: 3
  template:
    metadata:
      labels:
        app.kubernetes.io/name: curie
        app.kubernetes.io/instance: $RELEASE
    spec:
      restartPolicy: Never
      containers:
        - name: seed
          image: amazon/aws-cli:2.32.6
          imagePullPolicy: IfNotPresent
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              mkdir -p /tmp/aws
              aws configure set default.s3.addressing_style path
              endpoint="http://$RUSTFS_SVC:9000"
              aws --endpoint-url "\$endpoint" s3api head-bucket --bucket "$RUSTFS_BUCKET" >/dev/null 2>&1 || \
                aws --endpoint-url "\$endpoint" s3api create-bucket --bucket "$RUSTFS_BUCKET"
              aws --endpoint-url "\$endpoint" s3 cp /data/probe.tgz "s3://$RUSTFS_BUCKET/$BUNDLE_REF"
              echo "seeded $BUNDLE_REF"
          env:
            - name: AWS_ACCESS_KEY_ID
              value: $RUSTFS_ACCESS_KEY
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: $SECRET_NAME
                  key: rustfsSecretKey
            - name: AWS_DEFAULT_REGION
              value: us-east-1
            - name: AWS_CONFIG_FILE
              value: /tmp/aws/config
          volumeMounts:
            - name: bundle
              mountPath: /data
      volumes:
        - name: bundle
          configMap:
            name: e2e-bundle-seed
EOF

if ! kubectl wait --for=condition=complete job/e2e-bundle-seed \
    -n "$NAMESPACE" --timeout=120s; then
  kubectl logs job/e2e-bundle-seed -n "$NAMESPACE" || true
  fail "bundle seed Job did not complete"
fi
echo "bundle seeded: $BUNDLE_REF"

# --------------------------------------------------------------------------
# 5. Render + apply the bound sandbox Pod
# --------------------------------------------------------------------------
banner "RENDER bound sandbox Pod from SandboxTemplate $SANDBOX_TEMPLATE"
if [[ -n "$RUNNER_IMAGE" ]]; then
  echo "runner image override: $RUNNER_IMAGE"
else
  echo "runner image: real (from SandboxTemplate)"
fi
TEMPLATE_JSON="$(mktemp /tmp/e2e-sandboxtemplate.XXXXXX.json)"
kubectl get sandboxtemplate "$SANDBOX_TEMPLATE" -n "$NAMESPACE" -o json > "$TEMPLATE_JSON"
POD_JSON="$(mktemp /tmp/e2e-bound-pod.XXXXXX.json)"
build_bound_pod "$TEMPLATE_JSON" > "$POD_JSON"
kubectl apply -n "$NAMESPACE" -f "$POD_JSON"

banner "WAIT init pair Completed + runner Running"
wait_for_init_complete
echo "bound sandbox ready: init containers Completed, runner Running"

# --------------------------------------------------------------------------
# 6. Assertions
# --------------------------------------------------------------------------
banner "ASSERT runtime security (#56)"
run_assertions

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
echo
if [[ "$EXPECT_VULNERABLE" -eq 1 ]]; then
  echo "mode: negative-control (--expect-vulnerable)"
else
  echo "mode: default (expect secure)"
fi
if [[ "$RESULT" == "PASS" ]]; then
  echo "PASS: API Service NodePort and runtime security assertions"
  exit 0
else
  echo "FAIL"
  exit 1
fi
