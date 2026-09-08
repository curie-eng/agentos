#!/usr/bin/env bash
#
# Render-assertion test for issue #2432.
#
# Claim 5 of the security probe builds DATATIER_TARGETS only for stores with
# deploy: true. That is correct for the in-cluster ingress rail (there is no
# StatefulSet to wrap once postgres.deploy is false), but two things went
# missing on the BYO path:
#
#   1. The in-cluster Postgres:5432 target must not remain on Claim 5, or helm
#      test reports a 5432 failure against a Service that is no longer there
#      (or against an RDS endpoint that has no in-cluster deny/allow pair).
#   2. The probe must still name the operator-configured
#      postgres.host:postgres.port so the skip is a retarget, not a silent
#      disappearance of the 5432 check.
#
# Claim 5's allowed=reachable / denied=blocked pair cannot be pointed at RDS:
# there is no in-cluster NetworkPolicy on that host, so the denied pod would
# stay reachable and helm test would fail a correct BYO install. The skip
# names the BYO target instead. Rail 1 (runner default-deny egress) is what
# still blocks a sandbox from opening that port, and Claim 1 already proves
# that rail.
#
# Asserts:
#
#   1. default: DATATIER_TARGETS includes the in-chart Service at
#      postgres.port (5432), and POSTGRES_BYO_TARGET is empty.
#   2. postgres.port override: DATATIER_TARGETS follows the Service port the
#      same way assertion 13 pins rustfs.port (#1507), and the default 5432
#      target is gone.
#   3. BYO (deploy=false + host): DATATIER_TARGETS contains neither the
#      in-chart Service nor the RDS host, POSTGRES_BYO_TARGET is
#      host:port, and the probe script's Claim 5 path names that target on a
#      SKIP line. A render that still nc's 5432, or that drops the BYO host
#      without saying so, fails this gate.
#   4. BYO + custom port: POSTGRES_BYO_TARGET uses postgres.port, not a
#      hardcoded 5432.
#   5. NEGATIVE: postgres.deploy left true while postgres.host is set still
#      probes the in-chart Service. The helper ignores host on the in-chart
#      branch; a probe that flipped to the BYO host while the StatefulSet is
#      still deployed would Claim-5-fail (or worse, miss) the in-cluster rail.
#
# Runnable locally and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"

cleanup() {
  [[ -n "${TMP:-}" && -d "$TMP" ]] && rm -rf -- "$TMP"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

render_probe() {
  local name="$1"
  shift
  local out="$TMP/${name}.yaml"
  helm template curie "$CHART" \
    --show-only templates/security-probe.yaml \
    "$@" >"$out"
  printf '%s\n' "$out"
}

CHECKER="$TMP/check.py"
cat > "$CHECKER" <<'PY'
import pathlib
import sys

import yaml


def die(message):
    raise SystemExit(message)


def load_probe_job(path):
    docs = [doc for doc in yaml.safe_load_all(pathlib.Path(path).read_text()) if doc]
    jobs = [doc for doc in docs if doc.get("kind") == "Job"]
    if len(jobs) != 1:
        die(f"{path}: expected exactly one Job, found {len(jobs)}")
    containers = (
        jobs[0]
        .get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    probes = [container for container in containers if container.get("name") == "probe"]
    if len(probes) != 1:
        die(f"{path}: expected exactly one probe container, found {len(probes)}")
    return probes[0]


def env_entry(container, name):
    entries = [
        entry for entry in container.get("env", []) if entry.get("name") == name
    ]
    if len(entries) != 1:
        die(
            f"probe env {name!r} appears {len(entries)} times "
            f"(want exactly one literal value)"
        )
    if set(entries[0]) != {"name", "value"}:
        die(f"probe env {name!r} must be one literal value, got {entries[0]!r}")
    return entries[0]["value"]


def command_script(container):
    command = container.get("command") or []
    if not command:
        die("probe container has no command")
    return "\n".join(str(part) for part in command)


def main():
    path, mode = sys.argv[1], sys.argv[2]
    container = load_probe_job(path)
    targets = env_entry(container, "DATATIER_TARGETS").split()
    byo = env_entry(container, "POSTGRES_BYO_TARGET")
    script = command_script(container)

    if mode == "default":
        if "curie-postgres:5432" not in targets:
            die(
                "default DATATIER_TARGETS must include in-chart "
                f"'curie-postgres:5432'; got {targets!r}"
            )
        if byo != "":
            die(f"default POSTGRES_BYO_TARGET must be empty, got {byo!r}")
        if "Claim 5 does not probe in-cluster Postgres" not in script:
            die(
                "default probe script must still carry the BYO Claim 5 skip "
                "path, gated on POSTGRES_BYO_TARGET"
            )
        print("  ok: default Claim 5 still probes in-chart postgres:5432")
        return

    if mode == "port-override":
        if "curie-postgres:15432" not in targets:
            die(
                "DATATIER_TARGETS must follow postgres.port=15432 "
                f"('curie-postgres:15432'); got {targets!r}"
            )
        if "curie-postgres:5432" in targets:
            die(
                "DATATIER_TARGETS still contains the default 5432 target "
                f"after postgres.port=15432; got {targets!r}"
            )
        if byo != "":
            die(f"in-chart port override must leave POSTGRES_BYO_TARGET empty, got {byo!r}")
        print("  ok: Claim 5 follows postgres.port on the in-chart Service")
        return

    if mode == "byo":
        expected_byo = "my-rds.example.com:5432"
        forbidden = (
            "curie-postgres:5432",
            "curie-postgres:15432",
            expected_byo,
        )
        leftover = [item for item in targets if item in forbidden or item.startswith("curie-postgres:")]
        if leftover:
            die(
                "BYO DATATIER_TARGETS must not include in-chart Postgres or the "
                f"RDS host (Claim 5 would nc it and fail helm test); got leftover "
                f"{leftover!r} in {targets!r}"
            )
        if byo != expected_byo:
            die(
                f"BYO POSTGRES_BYO_TARGET must be {expected_byo!r} so the skip "
                f"names the operator host:port; got {byo!r}"
            )
        if "Claim 5 does not probe in-cluster Postgres" not in script:
            die(
                "probe script must SKIP in-cluster Postgres on the BYO path "
                "with an explicit Claim 5 skip line"
            )
        if "POSTGRES_BYO_TARGET" not in script:
            die(
                "probe script must read POSTGRES_BYO_TARGET at runtime so the "
                "BYO host:port is not a dead env var"
            )
        print("  ok: BYO Claim 5 skips in-cluster 5432 and names host:port")
        return

    if mode == "byo-port":
        expected_byo = "my-rds.example.com:15432"
        if byo != expected_byo:
            die(
                f"BYO POSTGRES_BYO_TARGET must follow postgres.port "
                f"({expected_byo!r}); got {byo!r}"
            )
        if "my-rds.example.com:5432" == byo:
            die("BYO target still hardcodes 5432 after postgres.port=15432")
        leftover = [
            item
            for item in targets
            if item.startswith("curie-postgres:") or item.startswith("my-rds.example.com:")
        ]
        if leftover:
            die(
                "BYO custom-port DATATIER_TARGETS still contains a Postgres "
                f"target Claim 5 would nc: {leftover!r} in {targets!r}"
            )
        print("  ok: BYO Claim 5 skip target follows postgres.port")
        return

    if mode == "host-ignored-when-deployed":
        if "curie-postgres:5432" not in targets:
            die(
                "postgres.host set while deploy stays true must still probe "
                f"the in-chart Service; got DATATIER_TARGETS={targets!r}"
            )
        if "my-rds.example.com:5432" in targets or byo == "my-rds.example.com:5432":
            die(
                "postgres.host must not retarget Claim 5 while the in-chart "
                f"StatefulSet is still deployed; DATATIER={targets!r} BYO={byo!r}"
            )
        if byo != "":
            die(
                "POSTGRES_BYO_TARGET must stay empty while postgres.deploy "
                f"is true; got {byo!r}"
            )
        print("  ok: postgres.host is ignored for Claim 5 while deploy is true")
        return

    die(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
PY

echo "=== Assertion 1: default in-chart Claim 5 still probes postgres:5432 ==="
DEFAULT_RENDER="$(render_probe default)"
python3 "$CHECKER" "$DEFAULT_RENDER" default

echo "=== Assertion 2: Claim 5 follows postgres.port (#2432 / #1507 parity) ==="
PORT_RENDER="$(render_probe port-override --set postgres.port=15432)"
python3 "$CHECKER" "$PORT_RENDER" port-override

echo "=== Assertion 3: BYO postgres skips in-cluster 5432 and names host:port ==="
BYO_RENDER="$(render_probe byo \
  --set postgres.deploy=false \
  --set postgres.host=my-rds.example.com)"
python3 "$CHECKER" "$BYO_RENDER" byo

echo "=== Assertion 4: BYO skip target follows postgres.port ==="
BYO_PORT_RENDER="$(render_probe byo-port \
  --set postgres.deploy=false \
  --set postgres.host=my-rds.example.com \
  --set postgres.port=15432)"
python3 "$CHECKER" "$BYO_PORT_RENDER" byo-port

echo "=== Assertion 5: postgres.host is ignored while deploy stays true ==="
HOST_SET_RENDER="$(render_probe host-set \
  --set postgres.host=my-rds.example.com)"
python3 "$CHECKER" "$HOST_SET_RENDER" host-ignored-when-deployed

echo "PASS: security probe skips in-cluster Postgres:5432 on BYO, names postgres.host:postgres.port on the skip path, follows postgres.port in-chart, and does not retarget Claim 5 while the in-chart StatefulSet is still deployed"
