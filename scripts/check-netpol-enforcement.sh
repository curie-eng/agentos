#!/usr/bin/env bash
# Assert Rail 1 (ADR-0067) actually ENFORCES, not merely that it is applied.
#
# The distinction is the whole point. Every NetworkPolicy the chart ships is
# applied on any cluster; whether it is EVALUATED depends on the CNI. kindnet
# (kind's default) and minikube's default bridge implement no NetworkPolicy
# controller, so a totally broken Rail 1 is indistinguishable from a correct one
# -- both let everything through, and every "can the sandbox reach X?" assertion
# passes. That is how an egress rule naming a Service ClusterIP, which can never
# match because kube-proxy DNATs to a pod IP before policy is evaluated, reached
# a real cluster (#1153).
#
# So this is structured as a NON-VACUITY check. It asserts a denied direction is
# genuinely blocked BEFORE trusting any allowed direction. If the deny does not
# hold, the CNI is not enforcing and the script FAILS -- it does not skip and it
# does not pass. A green run on a non-enforcing cluster would be worse than no
# check at all, because it reads as proof.
set -euo pipefail

NS="${1:-${CURIE_NETPOL_NS:-curie}}"
RELEASE="${2:-${CURIE_NETPOL_RELEASE:-curie}}"
APP="${3:-${CURIE_NETPOL_APP:-curie}}"
PROBE_IMAGE="${CURIE_NETPOL_PROBE_IMAGE:-curlimages/curl:8.10.1}"
# The deny target must actually LISTEN, so a blocked attempt is a timeout rather
# than an ambiguous connection-refused that a missing listener would also give.
TARGET_IMAGE="${CURIE_NETPOL_TARGET_IMAGE:-hashicorp/http-echo:1.0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="${CURIE_NETPOL_CHART:-$SCRIPT_DIR/../charts/curie}"

SANDBOX_POD="netpol-probe-sandbox"
OUTSIDE_POD="netpol-probe-outside"
DENY_TARGET_POD="netpol-probe-deny-target"
AGENT_A="netpol-agent-a"
AGENT_B="netpol-agent-b"
AGENT_A_POD="netpol-probe-${AGENT_A}"
AGENT_B_POD="netpol-probe-${AGENT_B}"
AGENT_A_TARGET_POD="netpol-probe-${AGENT_A}-target"
AGENT_B_TARGET_POD="netpol-probe-${AGENT_B}-target"

# Non-blocking on the way out: the run is over, nothing waits on the pods.
cleanup() {
  kubectl -n "$NS" delete pod "$SANDBOX_POD" "$OUTSIDE_POD" "$DENY_TARGET_POD" \
    "$AGENT_A_POD" "$AGENT_B_POD" "$AGENT_A_TARGET_POD" "$AGENT_B_TARGET_POD" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
# BLOCKING before the apply. A previous run's pod may still be terminating, and
# re-applying the same name against a deleting pod silently binds the OLD one --
# `wait --for=Ready` then passes on a pod that is on its way out and every exec
# after it fails for reasons that have nothing to do with policy.
cleanup_and_settle() {
  kubectl -n "$NS" delete pod "$SANDBOX_POD" "$OUTSIDE_POD" "$DENY_TARGET_POD" \
    "$AGENT_A_POD" "$AGENT_B_POD" "$AGENT_A_TARGET_POD" "$AGENT_B_TARGET_POD" \
    --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== Rail 1 enforcement check (namespace=$NS release=$RELEASE app=$APP) =="

kubectl -n "$NS" get networkpolicy "${RELEASE}-runner-default-deny-egress" >/dev/null 2>&1 \
  || fail "no ${RELEASE}-runner-default-deny-egress in $NS; is the chart installed?"

# The sandbox probe wears exactly the labels Rail 1 selects, so the policies
# under test apply to it. The outside probe and deny target wear none of the
# runner sandbox labels.
cleanup_and_settle
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $SANDBOX_POD
  labels:
    app.kubernetes.io/name: $APP
    app.kubernetes.io/instance: $RELEASE
    app.kubernetes.io/component: runner-sandbox
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $PROBE_IMAGE
      command: ["sleep", "600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $OUTSIDE_POD
  labels:
    app.kubernetes.io/name: netpol-probe-outside
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $PROBE_IMAGE
      command: ["sleep", "600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $DENY_TARGET_POD
  labels:
    app.kubernetes.io/name: netpol-probe-deny-target
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $TARGET_IMAGE
      args: ["-listen=:8000", "-text=reachable"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $AGENT_A_POD
  labels:
    app.kubernetes.io/name: $APP
    app.kubernetes.io/instance: $RELEASE
    app.kubernetes.io/component: runner-sandbox
    curietech.ai/agent: $AGENT_A
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $PROBE_IMAGE
      command: ["sleep", "600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $AGENT_B_POD
  labels:
    app.kubernetes.io/name: $APP
    app.kubernetes.io/instance: $RELEASE
    app.kubernetes.io/component: runner-sandbox
    curietech.ai/agent: $AGENT_B
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $PROBE_IMAGE
      command: ["sleep", "600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $AGENT_A_TARGET_POD
  labels:
    app.kubernetes.io/name: netpol-probe-target
spec:
  restartPolicy: Never
  containers:
    - name: target
      image: $TARGET_IMAGE
      args: ["-listen=:8000", "-text=agent-a"]
---
apiVersion: v1
kind: Pod
metadata:
  name: $AGENT_B_TARGET_POD
  labels:
    app.kubernetes.io/name: netpol-probe-target
spec:
  restartPolicy: Never
  containers:
    - name: target
      image: $TARGET_IMAGE
      args: ["-listen=:8000", "-text=agent-b"]
YAML

kubectl -n "$NS" wait --for=condition=Ready \
  "pod/$SANDBOX_POD" "pod/$OUTSIDE_POD" "pod/$DENY_TARGET_POD" \
  "pod/$AGENT_A_POD" "pod/$AGENT_B_POD" \
  "pod/$AGENT_A_TARGET_POD" "pod/$AGENT_B_TARGET_POD" --timeout=180s >/dev/null \
  || fail "probe pods did not become ready"

DENY_TARGET_IP="$(kubectl -n "$NS" get pod "$DENY_TARGET_POD" -o jsonpath='{.status.podIP}')"
[ -n "$DENY_TARGET_IP" ] || fail "could not read the deny target's pod IP"
AGENT_A_TARGET_IP="$(kubectl -n "$NS" get pod "$AGENT_A_TARGET_POD" -o jsonpath='{.status.podIP}')"
AGENT_B_TARGET_IP="$(kubectl -n "$NS" get pod "$AGENT_B_TARGET_POD" -o jsonpath='{.status.podIP}')"
[ -n "$AGENT_A_TARGET_IP" ] || fail "could not read the ${AGENT_A} target pod IP"
[ -n "$AGENT_B_TARGET_IP" ] || fail "could not read the ${AGENT_B} target pod IP"

cidr_for_ip() {
  local ip="$1"
  if [[ "$ip" == *:* ]]; then
    printf '%s/128' "$ip"
  else
    printf '%s/32' "$ip"
  fi
}

# The target pods exist before this upgrade so their exact pod IPs can become
# the scoped policy inputs. This proves the actual per-agent CIDR selectors,
# rather than a hand-written NetworkPolicy that merely resembles the chart.
helm upgrade "$RELEASE" "$CHART" --namespace "$NS" --reuse-values \
  --set-string "security.networkPolicy.agentAllowedEgress.${AGENT_A}[0].cidr=$(cidr_for_ip "$AGENT_A_TARGET_IP")" \
  --set-string "security.networkPolicy.agentAllowedEgress.${AGENT_A}[0].ports[0].protocol=TCP" \
  --set "security.networkPolicy.agentAllowedEgress.${AGENT_A}[0].ports[0].port=8000" \
  --set-string "security.networkPolicy.agentAllowedEgress.${AGENT_B}[0].cidr=$(cidr_for_ip "$AGENT_B_TARGET_IP")" \
  --set-string "security.networkPolicy.agentAllowedEgress.${AGENT_B}[0].ports[0].protocol=TCP" \
  --set "security.networkPolicy.agentAllowedEgress.${AGENT_B}[0].ports[0].port=8000" \
  >/dev/null || fail "Helm could not render and apply per-agent egress policies"

# ---------------------------------------------------------------------------
# GATE: the deny must hold. Everything below is meaningless without this.
# ---------------------------------------------------------------------------
# Rail 1 denies all sandbox egress except what an allow policy adds, and nothing
# allows the deny target. Reaching it means policy is not being evaluated.
if kubectl -n "$NS" exec "$SANDBOX_POD" -- \
     curl -s -m 8 -o /dev/null "http://${DENY_TARGET_IP}:8000/" 2>/dev/null; then
  fail "the sandbox reached a pod Rail 1 denies.

This is NOT a policy bug -- it means the CNI is not enforcing NetworkPolicy at
all, so every other assertion here would pass vacuously and this suite would
certify a broken Rail 1 as working.

  kind      needs 'disableDefaultCNI: true' plus a policy-enforcing CNI;
            the default kindnet implements no NetworkPolicy controller.
  minikube  needs 'minikube start --cni=calico'.
  k3s/k3d   enforce by default via kube-router.

Re-run against a cluster whose CNI enforces."
fi
echo "  ok  deny holds -- the CNI enforces NetworkPolicy (non-vacuity gate)"

# The outside probe must reach the same known listener before its inability to
# reach a connector can prove ingress enforcement. Otherwise a broken outside
# network would make every connector ingress denial pass vacuously.
if ! kubectl -n "$NS" exec "$OUTSIDE_POD" -- \
      curl -s -m 8 -o /dev/null "http://${DENY_TARGET_IP}:8000/" 2>/dev/null; then
  fail "the outside probe cannot reach the deny target; a broken outside network cannot certify connector ingress"
fi
echo "  ok  outside positive control reaches the deny target"

# ---------------------------------------------------------------------------
# Now the allow direction means something.
# ---------------------------------------------------------------------------
# The outside probe must resolve Service DNS before its inability to reach a
# connector can prove ingress enforcement. Otherwise a broken outside DNS
# path would make every connector ingress denial pass vacuously.
if ! kubectl -n "$NS" exec "$OUTSIDE_POD" -- \
      getent hosts kubernetes.default.svc.cluster.local >/dev/null 2>&1; then
  fail "the outside probe cannot resolve DNS; connector ingress denial would be vacuous"
fi
echo "  ok  outside positive control resolves DNS"

# DNS is the allow every sandbox depends on (curie-runner-allow-dns); without it
# a sandbox cannot resolve any Service name, which surfaces as a confusing
# connection failure rather than a policy error.
if ! kubectl -n "$NS" exec "$SANDBOX_POD" -- \
      getent hosts kubernetes.default.svc.cluster.local >/dev/null 2>&1; then
  fail "the sandbox cannot resolve DNS; ${RELEASE}-runner-allow-dns is not effective"
fi
echo "  ok  allow holds -- the sandbox can resolve DNS"

# Per-agent connector egress is a separate additive policy. The two policy
# targets differ, so a generic runner reaching either target would prove the
# agent selector is missing, and either named runner reaching the other's target
# would prove the selectors have been merged or widened.
assert_reaches() {
  local pod="$1"
  local ip="$2"
  local expected="$3"
  local output
  output="$(kubectl -n "$NS" exec "$pod" -- \
    curl -fsS -m 8 "http://${ip}:8000/" 2>/dev/null)" \
    || fail "${pod} could not reach its allowed per-agent target ${ip}:8000"
  [[ "$output" == *"$expected"* ]] \
    || fail "${pod} reached ${ip}:8000 but received the wrong target response"
}

assert_blocked() {
  local pod="$1"
  local ip="$2"
  if kubectl -n "$NS" exec "$pod" -- \
       curl -fsS -m 8 -o /dev/null "http://${ip}:8000/" 2>/dev/null; then
    fail "${pod} reached per-agent target ${ip}:8000 that its label must not allow"
  fi
}

assert_reaches "$AGENT_A_POD" "$AGENT_A_TARGET_IP" agent-a
assert_blocked "$AGENT_A_POD" "$AGENT_B_TARGET_IP"
assert_reaches "$AGENT_B_POD" "$AGENT_B_TARGET_IP" agent-b
assert_blocked "$AGENT_B_POD" "$AGENT_A_TARGET_IP"
assert_blocked "$SANDBOX_POD" "$AGENT_A_TARGET_IP"
assert_blocked "$SANDBOX_POD" "$AGENT_B_TARGET_IP"
echo "  ok  per-agent CIDR egress is isolated for ${AGENT_A}, ${AGENT_B}, and an unlabeled runner"

# Every connector Service is exercised in both directions. A missing Service
# makes the connector policy check vacuous and is therefore fatal.
CONNECTORS=()
while IFS= read -r svc; do
  [ -n "$svc" ] && CONNECTORS+=("$svc")
done < <(
  kubectl -n "$NS" get svc -l app.kubernetes.io/part-of="$RELEASE" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep -- "-mcp-" || true
)
CONNECTOR_COUNT="${#CONNECTORS[@]}"
(( CONNECTOR_COUNT > 0 )) \
  || fail "found 0 connector Services in $NS; connector NetworkPolicy enforcement would be vacuous"
echo "  ok  connector Service count: $CONNECTOR_COUNT"

for svc in "${CONNECTORS[@]}"; do
  kubectl -n "$NS" rollout status "deployment/$svc" --timeout=180s >/dev/null 2>&1 \
    || fail "connector Deployment $svc did not become ready"

  PORT="$(kubectl -n "$NS" get svc "$svc" -o jsonpath='{.spec.ports[0].port}')"
  kubectl -n "$NS" exec "$SANDBOX_POD" -- \
    curl -s -m 10 -o /dev/null "http://${svc}:${PORT}/" 2>/dev/null \
    || fail "the sandbox cannot reach connector Service $svc:$PORT.

Its egress rule is applied but not matching. The usual cause is an ipBlock of
the Service ClusterIP: kube-proxy DNATs the destination to a pod IP before
NetworkPolicy is evaluated, so such a rule can never match (ADR-0086)."
  echo "  ok  sandbox reaches connector $svc:$PORT"

  if kubectl -n "$NS" exec "$OUTSIDE_POD" -- \
       curl -s -m 10 -o /dev/null "http://${svc}:${PORT}/" 2>/dev/null; then
    fail "the outside probe reached connector Service $svc:$PORT; connector ingress is not restricted to runner sandboxes"
  fi
  echo "  ok  outside probe cannot reach connector $svc:$PORT"
done

echo "== Rail 1, connector egress, and connector ingress enforce =="
