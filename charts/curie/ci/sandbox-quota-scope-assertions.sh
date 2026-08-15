#!/usr/bin/env bash
#
# Render-assertion test for the sandbox ResourceQuota's scope (#1534).
#
# The tenant capacity ceiling is a PriorityClass-scoped ResourceQuota, so it
# bounds exactly the pods carrying one specific PriorityClass name. If that name
# is not the name the sandbox pods actually carry, the quota matches nothing and
# enforces nothing -- and it does so silently. The object exists, `kubectl get
# resourcequota` prints it, and `used: 0` reads as "nothing consumed" rather than
# "matching nothing". Kubernetes does not validate a scopeSelector's PriorityClass
# name against an existing object, so there is no error anywhere.
#
# That is not hypothetical. On a live install that had renamed its sandbox
# PriorityClass, a sandbox holding `limits.cpu: 1` was running while the quota
# reported `used: {limits.cpu: "0", pods: "0"}`, and the scoped class was absent
# from `kubectl get priorityclass` entirely. The whole ceiling was inert.
#
# The fix is to derive the scope from `priorityClasses.sandbox.name` -- the same
# value agent-sandbox.yaml stamps on the sandbox pod template -- rather than from
# an independent constant. These assertions pin that the two sides agree under
# every way an operator can set them.
#
# Six assertions:
#   (a) Default render: the quota's scope equals priorityClasses.sandbox.name.
#   (b) The invariant that actually matters: the quota's scope equals the
#       priorityClassName on the SandboxTemplate's pod spec. This is the one that
#       would have caught the live failure, because it compares the two rendered
#       objects rather than a value against itself.
#   (c) Renaming priorityClasses.sandbox.name propagates to the quota. This is
#       the regression: before the fix the scope stayed on the old default.
#   (d) An explicit sandboxPriorityClassName still wins, for a PriorityClass
#       managed outside this chart.
#   (e) With both empty, helm FAILS rather than rendering a quota scoped to "".
#       A quota scoped to an empty name matches no pods and enforces nothing --
#       exactly the silent shape this file exists to prevent.
#   (f) The quota is absent entirely when resourceQuota.enabled is false, so the
#       gate did not become unconditional.
set -euo pipefail

CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

QUOTA_TPL="templates/tenant-resourcequota.yaml"
SANDBOX_TPL="templates/agent-sandbox.yaml"

scope_of() {
  # Print the single PriorityClass value the rendered quota scopes on.
  python3 -c "
import sys, yaml
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if not doc or doc.get('kind') != 'ResourceQuota':
        continue
    for m in doc['spec']['scopeSelector']['matchExpressions']:
        if m['scopeName'] == 'PriorityClass':
            print(m['values'][0])
            sys.exit(0)
sys.exit('no PriorityClass scope found')
" "$1"
}

echo "=== (a) default: quota scope == priorityClasses.sandbox.name ==="
helm template rel "$CHART" --show-only "$QUOTA_TPL" > "$TMP/default-quota.yaml"
DEFAULT_SCOPE="$(scope_of "$TMP/default-quota.yaml")"
EXPECTED="$(python3 -c "
import yaml; print(yaml.safe_load(open('$CHART/values.yaml'))['priorityClasses']['sandbox']['name'])
")"
[ "$DEFAULT_SCOPE" = "$EXPECTED" ] || {
  echo "FAIL: quota scopes on '$DEFAULT_SCOPE', priorityClasses.sandbox.name is '$EXPECTED'" >&2
  exit 1
}
echo "  ok: $DEFAULT_SCOPE"

echo "=== (b) quota scope == the priorityClassName the sandbox pods carry ==="
helm template rel "$CHART" --show-only "$SANDBOX_TPL" > "$TMP/default-sandbox.yaml"
POD_PC="$(python3 -c "
import sys, yaml
for doc in yaml.safe_load_all(open('$TMP/default-sandbox.yaml')):
    if not doc or doc.get('kind') != 'SandboxTemplate':
        continue
    print(doc['spec']['podTemplate']['spec'].get('priorityClassName', ''))
    sys.exit(0)
sys.exit('no SandboxTemplate rendered')
")"
[ "$DEFAULT_SCOPE" = "$POD_PC" ] || {
  echo "FAIL: quota scopes on '$DEFAULT_SCOPE' but sandbox pods carry '$POD_PC'." >&2
  echo "      The quota matches no sandbox and silently enforces nothing." >&2
  exit 1
}
echo "  ok: both '$POD_PC'"

echo "=== (c) renaming priorityClasses.sandbox.name propagates to the quota ==="
helm template rel "$CHART" --show-only "$QUOTA_TPL" \
  --set priorityClasses.sandbox.name=tenant-a-sandbox > "$TMP/renamed-quota.yaml"
RENAMED_SCOPE="$(scope_of "$TMP/renamed-quota.yaml")"
[ "$RENAMED_SCOPE" = "tenant-a-sandbox" ] || {
  echo "FAIL: renamed to 'tenant-a-sandbox' but quota still scopes on '$RENAMED_SCOPE'." >&2
  echo "      This is #1534: the rename leaves the ceiling inert." >&2
  exit 1
}
helm template rel "$CHART" --show-only "$SANDBOX_TPL" \
  --set priorityClasses.sandbox.name=tenant-a-sandbox > "$TMP/renamed-sandbox.yaml"
grep -q "priorityClassName: tenant-a-sandbox" "$TMP/renamed-sandbox.yaml" || {
  echo "FAIL: rename did not reach the sandbox pod template" >&2; exit 1
}
echo "  ok: both followed the rename"

echo "=== (d) explicit sandboxPriorityClassName overrides the derivation ==="
helm template rel "$CHART" --show-only "$QUOTA_TPL" \
  --set priorityClasses.sandbox.name=ignored-here \
  --set resourceQuota.sandboxPriorityClassName=externally-managed > "$TMP/override-quota.yaml"
OVERRIDE_SCOPE="$(scope_of "$TMP/override-quota.yaml")"
[ "$OVERRIDE_SCOPE" = "externally-managed" ] || {
  echo "FAIL: explicit override ignored; scope is '$OVERRIDE_SCOPE'" >&2; exit 1
}
echo "  ok: $OVERRIDE_SCOPE"

echo "=== (e) both empty -> helm fails rather than scoping on \"\" ==="
if helm template rel "$CHART" --show-only "$QUOTA_TPL" \
     --set priorityClasses.sandbox.name= \
     --set resourceQuota.sandboxPriorityClassName= > "$TMP/empty.yaml" 2>"$TMP/empty.err"; then
  echo "FAIL: rendered a quota with no resolvable PriorityClass name:" >&2
  cat "$TMP/empty.yaml" >&2
  exit 1
fi
grep -qi "matches no pods\|silently enforces nothing" "$TMP/empty.err" || {
  echo "FAIL: helm failed, but not with the explanatory message:" >&2
  cat "$TMP/empty.err" >&2
  exit 1
}
echo "  ok: refused, with a reason"

echo "=== (f) quota absent when resourceQuota.enabled=false ==="
helm template rel "$CHART" --show-only "$QUOTA_TPL" \
  --set resourceQuota.enabled=false > "$TMP/disabled.yaml" 2>/dev/null || true
if grep -q "kind: ResourceQuota" "$TMP/disabled.yaml" 2>/dev/null; then
  echo "FAIL: quota rendered despite resourceQuota.enabled=false" >&2; exit 1
fi
echo "  ok: absent"

echo
echo "All sandbox quota scope assertions passed."
