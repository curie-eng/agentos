#!/usr/bin/env bash
#
# Render-assertion test for issue #351 (Langfuse pods die with
# CreateContainerConfigError). Kubernetes enforces `runAsNonRoot: true` by
# requiring a numeric uid it can verify is non-root, taken from either a numeric
# `runAsUser` in the securityContext or a numeric `USER` in the image. Both
# Langfuse images declare a NAMED user (nextjs for web, expressjs for worker),
# which the kubelet cannot resolve to a number, so `runAsNonRoot: true` with no
# numeric `runAsUser` makes the kubelet refuse to create the container.
#
# This asserts that the langfuse-web and langfuse-worker containers AND the web
# wait-for-postgres init container carry a numeric `runAsUser` (>= 1, pinned to
# the verified image uid 1001) alongside `runAsNonRoot: true`, so the pods
# actually start on an enforcing cluster. It fails loudly, naming the container, if `runAsUser` is absent while
# `runAsNonRoot: true` -- that is the exact #351 regression.
#
# Runnable locally (from anywhere) and from CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RENDER="$TMP/langfuse.yaml"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

echo "=== Rendering templates/langfuse.yaml ==="
helm template "$CHART" --show-only templates/langfuse.yaml > "$RENDER"

# Assert the securityContext of one named container in a Langfuse Deployment.
# $1 = rendered multi-doc YAML file, $2 = deployment name suffix
# (-langfuse-web / -langfuse-worker), $3 = expected numeric runAsUser,
# $4 = containers/initContainers, $5 = container name.
assert_container() {
  python3 -c '
import sys, yaml
path, suffix, want, collection, target = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]

dep = None
for doc in yaml.safe_load_all(open(path)):
    if not doc:
        continue
    if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name", "").endswith(suffix):
        dep = doc
        break
if dep is None:
    sys.stderr.write("no Deployment ending in %r found in rendered langfuse.yaml\n" % suffix)
    sys.exit(2)

matches = [
    item for item in dep["spec"]["template"]["spec"].get(collection, [])
    if item.get("name") == target
]
if len(matches) != 1:
    sys.stderr.write("deployment %r: expected one %s named %r, got %d\n" % (suffix, collection, target, len(matches)))
    sys.exit(3)
container = matches[0]
name = container.get("name")
sc = container.get("securityContext") or {}

if sc.get("runAsNonRoot") is not True:
    sys.stderr.write("container %r: securityContext.runAsNonRoot must be true; got %r\n" % (name, sc.get("runAsNonRoot")))
    sys.exit(3)

if "runAsUser" not in sc:
    sys.stderr.write("container %r: runAsNonRoot is true but runAsUser is ABSENT -- the kubelet cannot verify the named image user, this is the #351 CreateContainerConfigError regression\n" % name)
    sys.exit(3)

uid = sc["runAsUser"]
if not isinstance(uid, int) or isinstance(uid, bool):
    sys.stderr.write("container %r: runAsUser must be an integer; got %r (%s)\n" % (name, uid, type(uid).__name__))
    sys.exit(3)
if uid < 1:
    sys.stderr.write("container %r: runAsUser must be >= 1 (non-root); got %d\n" % (name, uid))
    sys.exit(3)
if uid != want:
    sys.stderr.write("container %r: runAsUser must equal %d (verified image uid); got %d\n" % (name, want, uid))
    sys.exit(3)

sys.stdout.write("  ok: %s carries runAsNonRoot: true + runAsUser: %d\n" % (name, uid))
' "$1" "$2" "$3" "$4" "$5" || fail "container assertion failed for deployment suffix '$2' / '$5' (see message above)"
}

echo "=== Assertion: langfuse-web carries a numeric runAsUser (== 1001) ==="
assert_container "$RENDER" "-langfuse-web" 1001 containers langfuse-web

echo "=== Assertion: wait-for-postgres carries a numeric runAsUser (== 1001) ==="
assert_container "$RENDER" "-langfuse-web" 1001 initContainers wait-for-postgres

echo "=== Assertion: langfuse-worker carries a numeric runAsUser (== 1001) ==="
assert_container "$RENDER" "-langfuse-worker" 1001 containers langfuse-worker

echo
echo "PASS: langfuse-web, wait-for-postgres, and langfuse-worker pin runAsUser: 1001 alongside runAsNonRoot: true (issue #351); the kubelet can verify non-root and the pods start."
