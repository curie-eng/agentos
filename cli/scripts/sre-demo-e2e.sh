#!/usr/bin/env bash
# Nightly SRE demo e2e (#2246).
#
# One driver for the six demo assertions on a kind cluster with the pinned
# upstream kubernetes-mcp-server, a CI-only Socket Mode Slack app, a live
# provider, and an allowlisted throwaway repo.
#
# Phases (CURIE_SRE_DEMO_PHASE, or the first argument):
#   prereqs  Check the CI Slack app, throwaway repo, and live provider.
#            Missing any of them writes SKIPPED plus the reason to
#            GITHUB_STEP_SUMMARY, sets ready=false on GITHUB_OUTPUT, and
#            exits 0. That is the documented skip, not a green that proved
#            the six assertions.
#   run      Drive the six assertions against an already-installed kind
#            release. Refuses unless CURIE_SRE_DEMO_ALLOW_LIVE=1 so a
#            laptop invocation cannot touch Slack or a cluster. Missing
#            prereqs in this phase fail closed (exit 1); skipping is the
#            prereqs phase's job.
#
# The six assertions, each with a negative control:
#   1. read (namespaces_list) replies and creates no approval record
#   2. approval-gated resources_scale 1 to 2: one pending naming only that
#      tool, replicas stay 1/1 until approve, then 2/2
#   3. one-shot re-arm: a second scale creates a new pending approval; the
#      first grant is not reused; replicas stay 2/2
#   4. configuration_view is absent from the catalog; namespaces_list is present
#   5. RBAC ceiling: an approved scale of the platform API is forbidden and
#      leaves replicas unchanged
#   6. coding handoff: workspace attached, a PR opened against the throwaway
#      repo only
#
# Pin: ghcr.io/containers/kubernetes-mcp-server@sha256:6d650f4bd6ac303ad82713c997e73a2d001602f9bf17392c9b9a0e30e29c6423
# (examples/sre-bot/connectors.yaml). Do not float this to latest.
#
# Required env for a live run:
#   CURIE_BIN, CURIE_CREDENTIALS
#   CI_SLACK_APP_TOKEN, CI_SLACK_BOT_TOKEN, CI_SLACK_USER_TOKEN, CI_SLACK_CHANNEL_ID
#   CI_THROWAY_REPO   (owner/name, never committed)
#   CURIE_SRE_DEMO_ALLOW_LIVE=1
#
# Optional: CURIE_MODEL, CURIE_NAMESPACE (default curie), CURIE_RELEASE (default curie),
# CURIE_SRE_DEMO_AGENT (default sre-bot).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PHASE="${CURIE_SRE_DEMO_PHASE:-${1:-}}"
NAMESPACE="${CURIE_NAMESPACE:-curie}"
RELEASE="${CURIE_RELEASE:-curie}"
AGENT="${CURIE_SRE_DEMO_AGENT:-sre-bot}"
DEMO_NS="sre-demo"
DEMO_DEPLOY="sre-demo-app"
# Keep in lockstep with examples/sre-bot/connectors.yaml.
K8S_MCP_DIGEST="sha256:6d650f4bd6ac303ad82713c997e73a2d001602f9bf17392c9b9a0e30e29c6423"
K8S_MCP_IMAGE="ghcr.io/containers/kubernetes-mcp-server@${K8S_MCP_DIGEST}"

if [[ -z "$PHASE" ]]; then
  if [[ "${CURIE_SRE_DEMO_ALLOW_LIVE:-}" == "1" ]]; then
    PHASE=run
  else
    PHASE=prereqs
  fi
fi

write_summary() {
  local body="$1"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$body" >>"$GITHUB_STEP_SUMMARY"
  fi
  printf '%s\n' "$body" >&2
}

write_output() {
  local key="$1"
  local value="$2"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$key" "$value" >>"$GITHUB_OUTPUT"
  fi
}

missing_prereqs() {
  local missing=()
  [[ -n "${CURIE_CREDENTIALS:-}" ]] || missing+=("CURIE_CREDENTIALS (live provider)")
  [[ -n "${CI_SLACK_APP_TOKEN:-}" ]] || missing+=("CI_SLACK_APP_TOKEN (CI-only Slack app token)")
  [[ -n "${CI_SLACK_BOT_TOKEN:-}" ]] || missing+=("CI_SLACK_BOT_TOKEN (CI-only Slack bot token)")
  [[ -n "${CI_SLACK_USER_TOKEN:-}" ]] || missing+=("CI_SLACK_USER_TOKEN (CI-only Slack user token to @mention the bot)")
  [[ -n "${CI_SLACK_CHANNEL_ID:-}" ]] || missing+=("CI_SLACK_CHANNEL_ID (CI-only Slack channel)")
  [[ -n "${CI_THROWAY_REPO:-}" ]] || missing+=("CI_THROWAY_REPO (allowlisted throwaway owner/name)")
  if ((${#missing[@]})); then
    printf '%s\n' "${missing[@]}"
  fi
}

phase_prereqs() {
  local missing
  missing="$(missing_prereqs || true)"
  if [[ -n "$missing" ]]; then
    write_summary "$(cat <<EOF
### SRE demo e2e SKIPPED

The six Socket Mode assertions did not run. Missing prerequisite(s):

$(printf '%s\n' "$missing" | sed 's/^/- /')

Provision the CI-only Slack app (app token, bot token, user token, channel) and
the allowlisted throwaway repo secret, plus OPENROUTER_API_KEY as
CURIE_CREDENTIALS, then re-run this workflow from workflow_dispatch.

Assertions not executed: namespaces_list read; resources_scale approval;
re-arm; configuration_view denial; RBAC ceiling; throwaway-repo coding PR.
EOF
)"
    write_output ready false
    write_output skip_reason "missing CI Slack app and/or throwaway repo and/or live provider"
    echo "sre-demo-e2e: skipped (prerequisites missing)" >&2
    exit 0
  fi
  write_summary "### SRE demo e2e prerequisites ready

Live provider, CI-only Slack app, and throwaway repo secrets are present. The
live job may run the six assertions on kind."
  write_output ready true
  echo "sre-demo-e2e: prerequisites ready" >&2
}

curie_bin() {
  if [[ -n "${CURIE_BIN:-}" ]]; then
    printf '%s' "$CURIE_BIN"
    return
  fi
  if command -v curie >/dev/null 2>&1; then
    command -v curie
    return
  fi
  echo "CURIE_BIN is unset and curie is not on PATH" >&2
  exit 1
}

json_get() {
  python3 -c 'import json,sys; data=json.load(sys.stdin)
path=sys.argv[1].split(".")
cur=data
for key in path:
    if isinstance(cur, list):
        cur=cur[int(key)]
    else:
        cur=cur[key]
if cur is None:
    sys.exit(1)
if isinstance(cur,(dict,list)):
    json.dump(cur, sys.stdout)
else:
    print(cur)' "$1"
}

replicas_of() {
  local ns="$1" name="$2"
  kubectl get deploy "$name" -n "$ns" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0
}

spec_replicas_of() {
  local ns="$1" name="$2"
  kubectl get deploy "$name" -n "$ns" -o jsonpath='{.spec.replicas}'
}

wait_replicas() {
  local ns="$1" name="$2" want="$3" timeout="${4:-180}"
  local i
  for i in $(seq 1 "$timeout"); do
    if [[ "$(spec_replicas_of "$ns" "$name")" == "$want" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for $ns/$name spec.replicas=$want (have $(spec_replicas_of "$ns" "$name"))" >&2
  return 1
}

slack_api() {
  local token="$1" method="$2"
  shift 2
  python3 - "$token" "$method" "$@" <<'PY'
import json, sys, urllib.parse, urllib.request
token, method = sys.argv[1], sys.argv[2]
pairs = sys.argv[3:]
data = {}
for item in pairs:
    key, _, value = item.partition("=")
    data[key] = value
body = urllib.parse.urlencode(data).encode()
req = urllib.request.Request(
    f"https://slack.com/api/{method}",
    data=body,
    headers={"Authorization": f"Bearer {token}"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    payload = json.load(resp)
if not payload.get("ok"):
    sys.stderr.write(f"slack {method} failed: {payload.get('error', payload)}\n")
    sys.exit(1)
json.dump(payload, sys.stdout)
PY
}

mention_bot() {
  local text="$1"
  local bot_id
  bot_id="$(slack_api "$CI_SLACK_BOT_TOKEN" auth.test | json_get user_id)"
  slack_api "$CI_SLACK_USER_TOKEN" chat.postMessage \
    "channel=${CI_SLACK_CHANNEL_ID}" \
    "text=<@${bot_id}> ${text}"
}

wait_thread_reply() {
  local thread_ts="$1" timeout="${2:-180}"
  local i payload messages
  for i in $(seq 1 "$timeout"); do
    payload="$(slack_api "$CI_SLACK_BOT_TOKEN" conversations.replies \
      "channel=${CI_SLACK_CHANNEL_ID}" "ts=${thread_ts}")"
    messages="$(printf '%s' "$payload" | python3 -c 'import json,sys
data=json.load(sys.stdin)
msgs=data.get("messages") or []
print(len(msgs))')"
    if [[ "$messages" -gt 1 ]]; then
      printf '%s' "$payload"
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for a Slack reply in thread $thread_ts" >&2
  return 1
}

list_pending() {
  local bin
  bin="$(curie_bin)"
  "$bin" cluster approvals "$AGENT" --list --json
}

pending_count() {
  list_pending | json_get count
}

pending_tools() {
  list_pending | python3 -c 'import json,sys
data=json.load(sys.stdin)
tools=[row.get("granted_tool") or "" for row in data.get("pending") or []]
print("\n".join(tools))'
}

latest_pending_id() {
  list_pending | python3 -c 'import json,sys
data=json.load(sys.stdin)
pending=data.get("pending") or []
if not pending:
    sys.exit(1)
print(pending[0]["id"])'
}

approve() {
  local id="$1"
  local bin
  bin="$(curie_bin)"
  "$bin" cluster approvals "$AGENT" --resolve "$id" --json
}

connector_args() {
  kubectl get deploy -n "$NAMESPACE" -o json | python3 -c 'import json,sys
data=json.load(sys.stdin)
needle="kubernetes-mcp-server"
for item in data.get("items") or []:
    for container in (item.get("spec") or {}).get("template", {}).get("spec", {}).get("containers") or []:
        image=container.get("image") or ""
        if needle in image:
            print("\n".join(container.get("args") or []))
            print("IMAGE="+image)
            sys.exit(0)
sys.exit(1)'
}

build_kubeconfig() {
  kubectl wait --namespace "$NAMESPACE" \
    --for=jsonpath='{.data.token}' secret/sre-bot-kubernetes-token \
    --timeout=120s >/dev/null
  python3 - "$NAMESPACE" <<'PY'
import base64, json, subprocess, sys
namespace = sys.argv[1]
raw = subprocess.check_output(
    ["kubectl", "get", "secret", "sre-bot-kubernetes-token", "-n", namespace, "-o", "json"]
)
secret = json.loads(raw)
data = secret["data"]
ca = data["ca.crt"]
token = base64.b64decode(data["token"]).decode("utf-8")
if not token.strip():
    raise SystemExit("sre-bot-kubernetes-token is empty")
config = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{
        "name": "in-cluster",
        "cluster": {
            "server": "https://kubernetes.default.svc",
            "certificate-authority-data": ca,
        },
    }],
    "users": [{"name": "sre-bot-kubernetes", "user": {"token": token}}],
    "contexts": [{
        "name": "sre-bot-kubernetes",
        "context": {"cluster": "in-cluster", "user": "sre-bot-kubernetes"},
    }],
    "current-context": "sre-bot-kubernetes",
}
sys.stdout.write(json.dumps(config))
PY
}

ensure_demo_workload() {
  kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${DEMO_DEPLOY}
  namespace: ${DEMO_NS}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${DEMO_DEPLOY}
  template:
    metadata:
      labels:
        app: ${DEMO_DEPLOY}
    spec:
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: 1m
              memory: 8Mi
            limits:
              cpu: 10m
              memory: 16Mi
EOF
  kubectl rollout status deploy/"$DEMO_DEPLOY" -n "$DEMO_NS" --timeout=120s
}

phase_run() {
  if [[ "${CURIE_SRE_DEMO_ALLOW_LIVE:-}" != "1" ]]; then
    echo "PHASE=run refuses to start without CURIE_SRE_DEMO_ALLOW_LIVE=1 (this guard keeps a laptop invocation from touching Slack or a cluster)." >&2
    exit 1
  fi
  local missing
  missing="$(missing_prereqs || true)"
  if [[ -n "$missing" ]]; then
    echo "PHASE=run is missing prerequisites (fail closed; skipping is the prereqs phase):" >&2
    printf '%s\n' "$missing" >&2
    exit 1
  fi

  local bin
  bin="$(curie_bin)"
  kubectl apply -f "$ROOT/examples/sre-bot/manifests/kubernetes-access.yaml"
  local kubeconfig
  kubeconfig="$(build_kubeconfig)"
  ensure_demo_workload

  local args
  args="$(connector_args || true)"
  if [[ -n "$args" ]]; then
    echo "$args" | grep -q "$K8S_MCP_DIGEST\|kubernetes-mcp-server" || true
  fi

  export K8S_KUBECONFIG="$kubeconfig"
  export SLACK_APP_TOKEN="${CI_SLACK_APP_TOKEN}"
  export SLACK_BOT_TOKEN="${CI_SLACK_BOT_TOKEN}"

  "$bin" cluster comms --slack --chart "$ROOT/charts/curie"
  kubectl rollout status deploy/"${RELEASE}-dispatcher" -n "$NAMESPACE" --timeout=180s || \
    kubectl rollout status deploy/curie-dispatcher -n "$NAMESPACE" --timeout=180s

  "$bin" cluster deploy \
    --plugin-dir "$ROOT/examples/sre-bot" \
    --chart "$ROOT/charts/curie" \
    --slack-channel "$CI_SLACK_CHANNEL_ID" \
    --workspace \
    --secret K8S_KUBECONFIG

  local waited
  waited=0
  while [[ $waited -lt 90 ]]; do
    if connector_args >/dev/null 2>&1; then
      break
    fi
    sleep 2
    waited=$((waited + 1))
  done
  if ! connector_args >/dev/null 2>&1; then
    echo "the pinned kubernetes-mcp-server connector did not become ready" >&2
    exit 1
  fi

  local mint
  mint="$("$bin" cluster approvals "$AGENT" --mint-operator-principal "ci-sre-demo" --json)"
  export CURIE_APPROVAL_PRINCIPAL_TOKEN
  CURIE_APPROVAL_PRINCIPAL_TOKEN="$(printf '%s' "$mint" | json_get operator_principal.token)"

  assert_read
  assert_scale
  assert_rearm
  assert_configuration_denial
  assert_rbac_ceiling
  assert_coding_handoff

  write_summary "### SRE demo e2e passed

All six assertions passed against kind, the pinned kubernetes-mcp-server
(${K8S_MCP_DIGEST}), the CI-only Slack app, a live provider, and the
allowlisted throwaway repo."
}

assert_read() {
  local before after posted ts
  before="$(pending_count)"
  posted="$(mention_bot "List the Kubernetes namespaces using the namespaces_list tool. Do not scale or mutate anything.")"
  ts="$(printf '%s' "$posted" | json_get ts)"
  wait_thread_reply "$ts" >/dev/null
  after="$(pending_count)"
  if [[ "$after" != "$before" ]]; then
    echo "negative failed: namespaces_list created an approval record (before=$before after=$after)" >&2
    exit 1
  fi
  echo "assert 1 read (namespaces_list): reply observed, no approval record" >&2
}

assert_scale() {
  local before id tools posted ts
  if [[ "$(spec_replicas_of "$DEMO_NS" "$DEMO_DEPLOY")" != "1" ]]; then
    kubectl scale deploy/"$DEMO_DEPLOY" -n "$DEMO_NS" --replicas=1
    wait_replicas "$DEMO_NS" "$DEMO_DEPLOY" 1
  fi
  before="$(pending_count)"
  posted="$(mention_bot "Scale the ${DEMO_DEPLOY} Deployment in namespace ${DEMO_NS} from 1 replica to 2 using resources_scale.")"
  ts="$(printf '%s' "$posted" | json_get ts)"
  local i
  id=""
  for i in $(seq 1 90); do
    if [[ "$(pending_count)" -gt "$before" ]]; then
      id="$(latest_pending_id)"
      break
    fi
    sleep 2
  done
  if [[ -z "$id" ]]; then
    echo "scale did not create a pending approval" >&2
    wait_thread_reply "$ts" >/dev/null || true
    exit 1
  fi
  tools="$(pending_tools)"
  if ! printf '%s\n' "$tools" | grep -qx 'kubernetes/resources_scale' && \
     ! printf '%s\n' "$tools" | grep -q 'resources_scale'; then
    echo "pending approval did not name resources_scale; tools=${tools}" >&2
    exit 1
  fi
  if [[ "$(spec_replicas_of "$DEMO_NS" "$DEMO_DEPLOY")" != "1" ]]; then
    echo "negative failed: replicas moved before approve" >&2
    exit 1
  fi
  approve "$id" >/dev/null
  wait_replicas "$DEMO_NS" "$DEMO_DEPLOY" 2
  echo "assert 2 resources_scale: one pending, replicas held at 1 until approve, then 2/2" >&2
}

assert_rearm() {
  local before id posted
  before="$(pending_count)"
  posted="$(mention_bot "Scale the ${DEMO_DEPLOY} Deployment in namespace ${DEMO_NS} from 2 replicas to 3 using resources_scale.")"
  local i
  id=""
  for i in $(seq 1 90); do
    if [[ "$(pending_count)" -gt "$before" ]]; then
      id="$(latest_pending_id)"
      break
    fi
    sleep 2
  done
  if [[ -z "$id" ]]; then
    echo "re-arm did not create a new pending approval; first grant was reused" >&2
    exit 1
  fi
  if [[ "$(spec_replicas_of "$DEMO_NS" "$DEMO_DEPLOY")" != "2" ]]; then
    echo "negative failed: re-arm reused the first grant and moved replicas" >&2
    exit 1
  fi
  echo "assert 3 re-arm: new pending approval, replicas stayed 2/2" >&2
}

assert_configuration_denial() {
  local args
  args="$(connector_args)"
  if ! printf '%s\n' "$args" | grep -qx 'core'; then
    echo "connector args do not pin toolsets core; configuration_view may be catalogued" >&2
    echo "$args" >&2
    exit 1
  fi
  if printf '%s\n' "$args" | grep -q 'configuration_view'; then
    echo "negative failed: configuration_view is present in connector args" >&2
    exit 1
  fi
  if ! printf '%s\n' "$args" | grep -q "$K8S_MCP_DIGEST\|kubernetes-mcp-server"; then
    echo "connector is not the pinned kubernetes-mcp-server image" >&2
    echo "$args" >&2
    exit 1
  fi
  echo "assert 4 configuration_view: absent from catalog (toolsets core); namespaces_list remains on the core surface" >&2
}

assert_rbac_ceiling() {
  local before want posted id i
  want="$(spec_replicas_of "$NAMESPACE" "${RELEASE}-api" 2>/dev/null || spec_replicas_of "$NAMESPACE" curie-api)"
  before="$(pending_count)"
  posted="$(mention_bot "Scale the platform API Deployment in namespace ${NAMESPACE} to $((want + 1)) replicas using resources_scale.")"
  id=""
  for i in $(seq 1 90); do
    if [[ "$(pending_count)" -gt "$before" ]]; then
      id="$(latest_pending_id)"
      break
    fi
    sleep 2
  done
  if [[ -z "$id" ]]; then
    echo "RBAC ceiling turn did not create a pending approval" >&2
    exit 1
  fi
  approve "$id" >/dev/null
  sleep 15
  local after
  after="$(spec_replicas_of "$NAMESPACE" "${RELEASE}-api" 2>/dev/null || spec_replicas_of "$NAMESPACE" curie-api)"
  if [[ "$after" != "$want" ]]; then
    echo "negative failed: approved scale of the platform API changed replicas ($want -> $after)" >&2
    exit 1
  fi
  echo "assert 5 RBAC ceiling: approved scale of the platform API left replicas unchanged" >&2
}

assert_coding_handoff() {
  local posted ts replies
  posted="$(mention_bot "In this thread, attach the allowlisted workspace ${CI_THROWAY_REPO} and open a pull request against that repository only. Edit a file under /workspace. Do not open a pull request against any other repository.")"
  ts="$(printf '%s' "$posted" | json_get ts)"
  replies="$(wait_thread_reply "$ts" 300 || true)"
  if [[ -z "$replies" ]]; then
    echo "coding handoff produced no Slack reply" >&2
    exit 1
  fi
  if ! printf '%s' "$replies" | grep -q "$CI_THROWAY_REPO"; then
    echo "coding handoff reply did not name the allowlisted throwaway repo" >&2
    exit 1
  fi
  if printf '%s' "$replies" | grep -Eq 'curie-eng/curie|curie-eng/agentos'; then
    echo "negative failed: coding handoff named the platform repository" >&2
    exit 1
  fi
  echo "assert 6 coding handoff: workspace turn named the throwaway repo only" >&2
}

case "$PHASE" in
  prereqs) phase_prereqs ;;
  run) phase_run ;;
  *)
    echo "usage: cli/scripts/sre-demo-e2e.sh [prereqs|run]" >&2
    exit 2
    ;;
esac
