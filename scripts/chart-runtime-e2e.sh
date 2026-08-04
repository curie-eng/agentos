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
# This harness makes that check one command.
#
# THE PATTERN (reusable -- this is NOT a #56 one-off)
# ---------------------------------------------------
# 1. Install a trimmed chart slice on k8scratch with RustFS and agent sandbox.
# 2. Seed a real bundle object into RustFS.
# 3. Render the SandboxTemplate.spec.podTemplate into a BOUND sandbox Pod
#    (CURIE_BUNDLE_REF pointing at the seeded object) and apply it -- so the
#    real bundle-fetch/bundle-extract init containers actually run.
# 4. Wait for the init pair to complete and the runner container to be Running.
# 5. `kubectl exec` into the runner and ASSERT on what it can see.
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
RUNNER_IMAGE=""
EXPECT_VULNERABLE=0
POD_NAME="e2e-bound-sandbox"
BUNDLE_REF="e2e/probe.tgz"

usage() {
  cat <<'EOF'
Usage: scripts/chart-runtime-e2e.sh [options]

Stands up a trimmed Curie chart slice on the k8scratch cluster, seeds a real
bundle into RustFS, renders a bound agent-sandbox Pod, runs its bundle-fetch/
extract init containers, and execs the runner to assert the #56 credential is
NOT readable off the shared bundle volume (and the bundle really was provisioned).

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
    kubectl delete ns "$NAMESPACE" --wait=true --timeout=120s >/dev/null 2>&1 || true
  else
    fail "namespace $NAMESPACE already exists and is NOT owned by this harness (missing label ${OWNED_LABEL}=true). Remove it manually or pick another --namespace."
  fi
fi

# Pre-create the namespace stamped with the ownership label so teardown/cleanup
# can only ever delete a namespace this script created.
create_owned_ns "$NAMESPACE"

banner "INSTALL trimmed chart"
install_chart() {
  helm install "$RELEASE" "$CHART_PATH" \
    -n "$NAMESPACE" --no-hooks \
    -f "$CHART_PATH/values-e2e-nogvisor.yaml" \
    -f "$CHART_PATH/values-e2e-harness.yaml"
}
# k8scratch is shared and slightly flaky: a spurious "namespaces not found" at
# install is usually API churn, so retry ONCE before failing.
if ! install_chart; then
  echo "helm install failed once (likely transient API churn on shared node); retrying in 5s..."
  sleep 5
  # Recreate the labeled namespace before retrying; only ever delete our own.
  if ns_is_owned "$NAMESPACE"; then
    kubectl delete ns "$NAMESPACE" --wait=true --timeout=60s >/dev/null 2>&1 || true
  fi
  create_owned_ns "$NAMESPACE"
  install_chart || fail "helm install failed twice"
fi

# --------------------------------------------------------------------------
# 2. Wait for RustFS Running. Gate on the pod, not Helm release status.
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
# 3. Seed a real bundle into RustFS
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
PROBE_B64="$(base64 -w0 "$SEED_DIR/probe.tgz" 2>/dev/null || base64 "$SEED_DIR/probe.tgz" | tr -d '\n')"

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
# 4. Render + apply the bound sandbox Pod
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
# 5. Assertions
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
  echo "PASS"
  exit 0
else
  echo "FAIL"
  exit 1
fi
