#!/usr/bin/env bash
#
# Render-assertion test for `valkey.existingSecret` reaching Langfuse (#2052).
#
# The chart's stated invariant (charts/curie/CLAUDE.md, "Every backing store
# follows the same toggle + BYO idiom") is that flipping `<store>.deploy` to
# false repoints EVERY consumer at the BYO `host`/`port`/`auth`/`existingSecret`
# fields on the same block. Valkey had two consumer groups and only one of them
# honoured it: `curie.env.valkey` (api + worker) read
# `valkey.existingSecret | default <chart secret>`, while `curie.langfuse.env`
# hardcoded the chart Secret for REDIS_AUTH.
#
# The consequence is silent and asymmetric, which is why it is worth a gate. On
# a BYO valkey install (`deploy=false` + `host` + `existingSecret`) the chart
# Secret still renders and still holds the chart-generated valkeyPassword, so
# nothing errors at template or install time. The api and worker authenticate
# against the real instance and stay healthy; only Langfuse presents the wrong
# password, so trace ingestion dies while every other component reports green.
# There is no failing manifest, no failing preflight, and no unhealthy pod to
# point at the cause -- exactly the shape of the rustfs endpoint bug already
# tombstoned in the same `curie.langfuse.env` block.
#
# Asserts:
#
#   1. DEFAULT render: REDIS_AUTH on BOTH langfuse containers resolves to the
#      chart's own Secret, key `valkeyPassword`. The no-regression case -- the
#      fix must not repoint an install that never set existingSecret.
#   2. `valkey.existingSecret=acme-valkey`: REDIS_AUTH on BOTH langfuse
#      containers resolves to `acme-valkey`. Both, because web and worker are
#      separate Deployments that each include the shared env helper; a fix
#      applied to one include site and not the other renders half a release.
#   3. Parity in the SAME render: VALKEY_PASSWORD on the api and worker
#      containers also resolves to `acme-valkey`. This is the pair that was
#      split, so both sides are asserted together -- asserting only the langfuse
#      half would let a future edit "fix" the split by breaking the app services
#      instead.
#   4. The realistic full BYO shape (`deploy=false` + `host` + `existingSecret`):
#      no valkey StatefulSet renders AND langfuse still resolves `acme-valkey`.
#      This is the exact supported configuration the bug broke, and it is not
#      the same render as (2) -- (2) keeps the in-chart valkey, so it alone
#      cannot prove the deploy=false path.
#   5. NEGATIVE CONTROL: under the BYO render, the chart Secret name must NOT
#      appear as the REDIS_AUTH secretKeyRef on either langfuse container.
#      Without this, assertions 2 and 4 would still pass against a template that
#      emitted REDIS_AUTH twice or fell back to the chart Secret, and the gate
#      would be vacuous.
#
# Deliberately NOT asserted: POSTGRES_PASSWORD, SALT and ENCRYPTION_KEY in the
# same `curie.langfuse.env` block still hardcode the chart Secret even though
# `postgres.existingSecret` and `langfuse.existingSecret` exist. That is a known
# gap tracked separately; pinning their current values here would freeze the bug
# into the gate.
#
# Every render goes through `--output-dir`, never a stdout pipe: piping
# `helm template` in this environment silently truncates a large render at
# exit 0 with empty stderr, which reads as a passing assertion against manifests
# that were never examined. Structural checks go through PyYAML rather than
# grep, for the reason dispatcher-api-wiring-assertions.sh gives -- a
# line-oriented reader mis-reads a requoted value or a reordered key.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

render() {
  local name="$1"
  shift
  RENDER_DIR="$TMP/$name"
  rm -rf "$RENDER_DIR"
  helm template rel "$CHART" --output-dir "$RENDER_DIR" "$@" >/dev/null \
    || fail "helm template failed for render '$name'"
}

echo "=== Rendering Langfuse (defaults) ==="
render default
DEFAULT_DIR="$RENDER_DIR/curie/templates"

echo "=== Rendering Langfuse (valkey.existingSecret=acme-valkey) ==="
render byo --set valkey.existingSecret=acme-valkey
BYO_DIR="$RENDER_DIR/curie/templates"

echo "=== Rendering Langfuse (valkey.deploy=false + host + existingSecret) ==="
render byo-full \
  --set valkey.deploy=false \
  --set valkey.host=redis.acme.internal \
  --set valkey.existingSecret=acme-valkey
BYO_FULL_DIR="$RENDER_DIR/curie/templates"

# ---------------------------------------------------------------------- 4a
# A missing manifest file is --output-dir's signal that the whole template
# rendered nothing (the same check direct-passthrough-existing-secret-
# assertions.sh uses); an empty document would not be distinguishable.
if [[ -s "$BYO_FULL_DIR/valkey.yaml" ]]; then
  fail "[4a] valkey.deploy=false still rendered templates/valkey.yaml"
fi
echo "  [4a] valkey.deploy=false renders no in-chart valkey manifest: OK"

# ------------------------------------------------------------- 1, 2, 3, 4b, 5
DEFAULT_DIR="$DEFAULT_DIR" BYO_DIR="$BYO_DIR" BYO_FULL_DIR="$BYO_FULL_DIR" \
python3 <<'PY'
import os
import sys

import yaml

DEFAULT_DIR = os.environ["DEFAULT_DIR"]
BYO_DIR = os.environ["BYO_DIR"]
BYO_FULL_DIR = os.environ["BYO_FULL_DIR"]

# `helm template rel <chart>` -> fullname `rel-curie`, so the chart's own
# Secret is `rel-curie-secrets`. Hardcoded rather than derived: the point of
# the negative control is that this exact name must NOT appear.
CHART_SECRET_NAME = "rel-curie-secrets"
BYO_SECRET_NAME = "acme-valkey"

failures = []


def load_docs(path):
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [d for d in yaml.safe_load_all(f) if d]


def find_containers(obj, acc):
    if isinstance(obj, dict):
        containers = obj.get("containers")
        if isinstance(containers, list):
            acc.extend(containers)
        for v in obj.values():
            find_containers(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            find_containers(item, acc)


def find_env(manifest, container_name, env_name):
    """Return (secretKeyRef-or-None, match-count) for one env entry.

    Searches every container across every document in the file, so one helper
    covers langfuse.yaml's two Deployments (langfuse-web / langfuse-worker) as
    well as api.yaml and worker.yaml.
    """
    containers = []
    for d in load_docs(manifest):
        find_containers(d, containers)
    matched = [c for c in containers
               if isinstance(c, dict) and c.get("name") == container_name]
    entries = [e for c in matched for e in (c.get("env") or [])
               if e.get("name") == env_name]
    if len(entries) != 1:
        return None, len(entries)
    ref = (entries[0].get("valueFrom") or {}).get("secretKeyRef")
    return ref, 1


def check_ref(aid, manifest, container, env_name, expected_secret, expected_key, ctx):
    ref, n = find_env(manifest, container, env_name)
    if n == 0:
        failures.append(f"[{aid}] {ctx}: {env_name} did not render on container "
                        f"{container!r} in {manifest}")
        return
    if n > 1:
        failures.append(f"[{aid}] {ctx}: {env_name} rendered {n} times on container "
                        f"{container!r}, expected exactly 1")
        return
    if not ref:
        failures.append(f"[{aid}] {ctx}: {env_name} has no valueFrom.secretKeyRef "
                        "(an inline value would put this credential in the manifest)")
        return
    if ref.get("name") != expected_secret:
        failures.append(f"[{aid}] {ctx}: {env_name} secretKeyRef.name = "
                        f"{ref.get('name')!r}, expected {expected_secret!r}")
    if ref.get("key") != expected_key:
        failures.append(f"[{aid}] {ctx}: {env_name} secretKeyRef.key = "
                        f"{ref.get('key')!r}, expected {expected_key!r}")


# Paired with a literal id suffix so every per-container assertion id follows the
# same a/b convention as the single-container ones (3a/3b) in this file.
LANGFUSE_CONTAINERS = [("a", "langfuse-web"), ("b", "langfuse-worker")]

# ---- 1: default render still resolves to the chart's own Secret. ----
for suffix, c in LANGFUSE_CONTAINERS:
    check_ref(f"1{suffix}", f"{DEFAULT_DIR}/langfuse.yaml", c,
              "REDIS_AUTH", CHART_SECRET_NAME, "valkeyPassword",
              f"default render, {c}")

# ---- 2: valkey.existingSecret reaches BOTH langfuse containers. ----
for suffix, c in LANGFUSE_CONTAINERS:
    check_ref(f"2{suffix}", f"{BYO_DIR}/langfuse.yaml", c,
              "REDIS_AUTH", BYO_SECRET_NAME, "valkeyPassword",
              f"valkey.existingSecret set, {c}")

# ---- 3: parity with the app services in the SAME render. This is the pair
#         that was split, so both halves are asserted together. ----
check_ref("3a", f"{BYO_DIR}/api.yaml", "api", "VALKEY_PASSWORD",
          BYO_SECRET_NAME, "valkeyPassword", "valkey.existingSecret set, api")
check_ref("3b", f"{BYO_DIR}/worker.yaml", "worker", "VALKEY_PASSWORD",
          BYO_SECRET_NAME, "valkeyPassword", "valkey.existingSecret set, worker")

# ---- 4b: the realistic full BYO shape (deploy=false + host + existingSecret)
#          -- the exact supported configuration the bug broke. ----
for suffix, c in LANGFUSE_CONTAINERS:
    check_ref(f"4b{suffix}", f"{BYO_FULL_DIR}/langfuse.yaml", c,
              "REDIS_AUTH", BYO_SECRET_NAME, "valkeyPassword",
              f"valkey.deploy=false + host + existingSecret, {c}")

# ---- 5: NEGATIVE CONTROL. Proves the assertion catches the #2052 bug rather
#         than passing vacuously: under both BYO renders the chart Secret must
#         not back REDIS_AUTH on either langfuse container. ----
for label, d in (("byo", BYO_DIR), ("byo-full", BYO_FULL_DIR)):
    for _suffix, c in LANGFUSE_CONTAINERS:
        ref, n = find_env(f"{d}/langfuse.yaml", c, "REDIS_AUTH")
        if n == 1 and ref and ref.get("name") == CHART_SECRET_NAME:
            failures.append(
                f"[5] negative control ({label}, {c}): REDIS_AUTH still resolves to the "
                f"chart-managed Secret {CHART_SECRET_NAME!r} with valkey.existingSecret="
                f"{BYO_SECRET_NAME!r} set. This is issue #2052: the app services read the BYO "
                "Secret while Langfuse alone reads the chart one, so Langfuse presents the "
                "wrong password and trace ingestion dies silently with the rest of the "
                "release healthy.")

if failures:
    for msg in failures:
        print(f"FAIL {msg}", file=sys.stderr)
    print(f"{len(failures)} of 10 python-side assertions failed", file=sys.stderr)
    sys.exit(1)

print("  [1] default render: REDIS_AUTH -> chart Secret on web + worker: OK")
print("  [2] valkey.existingSecret: REDIS_AUTH -> BYO Secret on web + worker: OK")
print("  [3] same render: VALKEY_PASSWORD -> BYO Secret on api + worker: OK")
print("  [4b] deploy=false + host + existingSecret: langfuse -> BYO Secret: OK")
print("  [5] negative control: chart Secret never backs REDIS_AUTH under BYO: OK")
PY

echo
echo "PASS: valkey.existingSecret reaches every consumer, Langfuse included (#2052)."
