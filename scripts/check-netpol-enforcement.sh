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

SANDBOX_POD="netpol-probe-sandbox"
OUTSIDE_POD="netpol-probe-outside"

# Non-blocking on the way out: the run is over, nothing waits on the pods.
cleanup() {
  kubectl -n "$NS" delete pod "$SANDBOX_POD" "$OUTSIDE_POD" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
# BLOCKING before the apply. A previous run's pod may still be terminating, and
# re-applying the same name against a deleting pod silently binds the OLD one --
# `wait --for=Ready` then passes on a pod that is on its way out and every exec
# after it fails for reasons that have nothing to do with policy.
cleanup_and_settle() {
  kubectl -n "$NS" delete pod "$SANDBOX_POD" "$OUTSIDE_POD" \
    --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== Rail 1 enforcement check (namespace=$NS release=$RELEASE app=$APP) =="

kubectl -n "$NS" get networkpolicy "${RELEASE}-runner-default-deny-egress" >/dev/null 2>&1 \
  || fail "no ${RELEASE}-runner-default-deny-egress in $NS; is the chart installed?"

# The sandbox probe wears exactly the labels Rail 1 selects, so the policies
# under test apply to it. The outside probe wears none, and is the target the
# sandbox must NOT reach.
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
      image: $TARGET_IMAGE
      args: ["-listen=:8000", "-text=reachable"]
YAML

kubectl -n "$NS" wait --for=condition=Ready "pod/$SANDBOX_POD" "pod/$OUTSIDE_POD" --timeout=180s >/dev/null \
  || fail "probe pods did not become ready"

OUTSIDE_IP="$(kubectl -n "$NS" get pod "$OUTSIDE_POD" -o jsonpath='{.status.podIP}')"
[ -n "$OUTSIDE_IP" ] || fail "could not read the outside probe's pod IP"

# ---------------------------------------------------------------------------
# GATE: the deny must hold. Everything below is meaningless without this.
# ---------------------------------------------------------------------------
# Rail 1 denies all sandbox egress except what an allow policy adds, and nothing
# allows the outside probe. Reaching it means policy is not being evaluated.
if kubectl -n "$NS" exec "$SANDBOX_POD" -- \
     curl -s -m 8 -o /dev/null "http://${OUTSIDE_IP}:8000/" 2>/dev/null; then
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

# ---------------------------------------------------------------------------
# Now the allow direction means something.
# ---------------------------------------------------------------------------
# DNS is the allow every sandbox depends on (curie-runner-allow-dns); without it
# a sandbox cannot resolve any Service name, which surfaces as a confusing
# connection failure rather than a policy error.
if ! kubectl -n "$NS" exec "$SANDBOX_POD" -- \
      getent hosts kubernetes.default.svc.cluster.local >/dev/null 2>&1; then
  fail "the sandbox cannot resolve DNS; ${RELEASE}-runner-allow-dns is not effective"
fi
echo "  ok  allow holds -- the sandbox can resolve DNS"

# Any connector Service present is exercised too: this is the rule that was
# silently broken by the ipBlock form, so it is the one most worth asserting.
CONNECTORS="$(kubectl -n "$NS" get svc -l app.kubernetes.io/part-of="$RELEASE" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -- "-mcp-" || true)"
if [ -z "$CONNECTORS" ]; then
  echo "  --  no connector Services in $NS; skipping the connector reachability leg"
else
  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    PORT="$(kubectl -n "$NS" get svc "$svc" -o jsonpath='{.spec.ports[0].port}')"
    kubectl -n "$NS" exec "$SANDBOX_POD" -- \
      curl -s -m 10 -o /dev/null "http://${svc}:${PORT}/" 2>/dev/null \
      || fail "the sandbox cannot reach connector Service $svc:$PORT.

Its egress rule is applied but not matching. The usual cause is an ipBlock of
the Service ClusterIP: kube-proxy DNATs the destination to a pod IP before
NetworkPolicy is evaluated, so such a rule can never match (ADR-0086)."
    echo "  ok  sandbox reaches connector $svc:$PORT"
  done <<<"$CONNECTORS"
fi

echo "== Rail 1 enforces, and every allowed path is reachable =="
