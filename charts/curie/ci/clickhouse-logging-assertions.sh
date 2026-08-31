#!/usr/bin/env bash
#
# Render-assertion test for the ClickHouse self-telemetry defaults. Proves:
#
#   1. DEFAULT render: a config.d overlay ConfigMap sets the logger to
#      `warning` AND turns on console logging, because the image logs only to
#      files under /var/log/clickhouse-server/ -- so without it `kubectl logs`
#      on the pod is empty and the diagnostics die with the container.
#   2. text_log is KEPT but level-filtered, not removed. The image ships
#      <text_log><level>trace</level>, and that level is the actual firehose;
#      filtering it cuts volume ~4 orders of magnitude while leaving the table
#      queryable. A queryable text_log is what diagnosed the incident below,
#      and unlike the log file it outlives the pod.
#   3. The timer-driven samplers (trace_log, metric_log,
#      asynchronous_metric_log) ARE removed. They have no level filter, so
#      on/off is the only lever, and metric_log is the table whose merge wedged.
#   4. Every surviving table carries a deleting TTL -- including
#      processors_profile_log, which a live boot revealed the image also
#      creates and which is easy to miss while it is still small.
#   5. The StatefulSet actually consumes it: a `config` volume, a subPath mount
#      into config.d (mounting the directory would hide the image's own
#      docker_related_config.xml), and a checksum annotation so an edit rolls
#      the pod rather than leaving the fix un-read by the running server.
#   6. OVERRIDE: `systemLogs.enabled=true` restores the three samplers, still
#      TTL-bounded, and `retentionDays` reaches text_log too.
#   7. The overlay is well-formed XML by ClickHouse's standard, not just
#      Python's. ElementTree accepts `--` inside a comment; the Poco SAX parser
#      ClickHouse uses rejects it and the server exits 232 at boot. That exact
#      mistake was made while developing this change, so it is asserted.
#   8. `clickhouse.deploy=false` renders nothing, and `persistence.enabled=false`
#      still gets its `data` emptyDir alongside the new `config` volume.
#   9. `clickhouse.existingSecret` reaches Langfuse, not just the in-chart
#      server. Both langfuse containers' CLICKHOUSE_PASSWORD and the StatefulSet's
#      own must resolve to the SAME named Secret, with the default render still
#      resolving to the chart's own. Langfuse hardcoded the chart Secret (#2052),
#      which fails silently in two directions: with `deploy=false` + `host` +
#      `existingSecret` only Langfuse authenticated against the wrong password,
#      and with `deploy=true` + `existingSecret` the in-chart server and Langfuse
#      disagreed about the password entirely (split-brain auth). Either way the
#      release comes up green and trace ingestion is simply gone.
#
# Why this is worth a gate: stock ClickHouse config is sized for a dedicated
# analytics box, and the chart runs it as an embedded store for kilobytes of
# Langfuse traces. Left at image defaults it logs at `trace` and retains 30 days
# of its own telemetry. Measured on a real install: 2.72 GiB of self-telemetry
# against 31 KiB of Langfuse data, filling a 3Gi volume in eight days, ending in
# a system.metric_log merge that exceeded the memory limit and was retried
# forever -- each failure writing a ~4 KiB stack trace into text_log, which then
# needed merging itself. Daily CPU went 17% -> 98% over eight days with no
# change in traffic, and every agent turn escalated as an opaque `runner-error`
# because the sandbox could not win enough CPU to bind its claim inside 90s.
#
# A regression here is silent for about a week and then takes the node out, so
# it is asserted rather than trusted.
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
  local chart="$2"
  shift 2

  RENDER_DIR="$TMP/$name"
  helm template rel "$chart" --output-dir "$RENDER_DIR" "$@"
}

manifest_for() {
  local manifest
  manifest="$(find "$1" -type f -path "*/templates/clickhouse.yaml" -print -quit)"
  [[ -n "$manifest" ]] || fail "ClickHouse template was not written to $1"
  printf '%s\n' "$manifest"
}

echo "=== Rendering ClickHouse (defaults) ==="
render default "$CHART"
DEFAULT="$(manifest_for "$RENDER_DIR")"
DEFAULT_TEMPLATES="$RENDER_DIR/curie/templates"

echo "=== Rendering ClickHouse (systemLogs.enabled=true, retentionDays=7) ==="
render verbose "$CHART" \
  --set clickhouse.systemLogs.enabled=true \
  --set clickhouse.systemLogs.retentionDays=7
VERBOSE="$(manifest_for "$RENDER_DIR")"

echo "=== Rendering ClickHouse (persistence disabled) ==="
render nopersist "$CHART" --set clickhouse.persistence.enabled=false
NOPERSIST="$(manifest_for "$RENDER_DIR")"

echo "=== Rendering ClickHouse (deploy=false) ==="
render off "$CHART" \
  --set clickhouse.deploy=false \
  --set clickhouse.host=clickhouse.example.com
OFF="$RENDER_DIR"

echo "=== Rendering ClickHouse (existingSecret=acme-ch) ==="
render existing-secret "$CHART" --set clickhouse.existingSecret=acme-ch
EXISTING_SECRET_TEMPLATES="$RENDER_DIR/curie/templates"

CHECKSUM_VALUES=(
  --set clickhouse.logLevel=warning
  --set clickhouse.systemLogs.enabled=false
  --set clickhouse.systemLogs.retentionDays=30
)
MUTATED_CHART="$TMP/checksum-chart"
cp -a "$CHART" "$MUTATED_CHART"

echo "=== Rendering ClickHouse checksum baseline ==="
render checksum-original "$MUTATED_CHART" "${CHECKSUM_VALUES[@]}"
CHECKSUM_ORIGINAL="$(manifest_for "$RENDER_DIR")"

python3 - "$MUTATED_CHART/templates/_helpers.tpl" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
original = path.read_text()
mutated = original.replace("<console>1</console>", "<console>0</console>", 1)
assert mutated != original, "checksum mutation target was not found"
path.write_text(mutated)
PY

echo "=== Rendering ClickHouse checksum mutation ==="
render checksum-mutated "$MUTATED_CHART" "${CHECKSUM_VALUES[@]}"
CHECKSUM_MUTATED="$(manifest_for "$RENDER_DIR")"

python3 - "$CHECKSUM_ORIGINAL" "$CHECKSUM_MUTATED" <<'PY'
import sys, yaml

def config_and_checksum(path):
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    cm = next((d for d in docs if d["kind"] == "ConfigMap"), None)
    sts = next((d for d in docs if d["kind"] == "StatefulSet"), None)
    assert cm is not None, f"{path}: no ClickHouse config ConfigMap rendered"
    assert sts is not None, f"{path}: no ClickHouse StatefulSet rendered"
    annotations = sts["spec"]["template"]["metadata"].get("annotations") or {}
    checksum = annotations.get("checksum/config")
    assert checksum, f"{path}: no checksum/config annotation rendered"
    return cm["data"]["curie-logging.xml"], checksum

original_body, original_checksum = config_and_checksum(sys.argv[1])
mutated_body, mutated_checksum = config_and_checksum(sys.argv[2])

assert original_body != mutated_body, "checksum mutation did not change the ConfigMap body"
assert original_checksum != mutated_checksum, (
    "ConfigMap body changed but checksum/config did not. The pod would not roll."
)

print("  ConfigMap body mutation updates checksum: OK")
PY

# ---------------------------------------------------------------- 1, 2, 4, 6
python3 - "$DEFAULT" "$VERBOSE" <<'PY'
import sys, yaml, xml.etree.ElementTree as ET

import re

# Timer-driven samplers: no level filter exists for them, so on/off is the only
# lever. metric_log is the table whose merge wedged and started the spiral.
SAMPLERS = ["trace_log", "metric_log", "asynchronous_metric_log"]
# Always present, always TTL'd. processors_profile_log is on this list because a
# live boot showed the image creates it -- easy to miss while it is still small.
KEEP     = ["query_log", "part_log", "error_log", "processors_profile_log"]

def check_xml(raw, path):
    """Parse strictly enough to catch what ClickHouse's parser rejects.

    ElementTree is MORE PERMISSIVE than the Poco SAX parser ClickHouse uses. It
    happily accepts `--` inside an XML comment, which XML forbids and which
    makes the server exit 232 on boot with:

        Failed to merge config with '...curie-logging.xml': SAXParseException:
        Invalid token

    That is a total outage from a comment, and it got past an ElementTree-only
    check during development. Assert it explicitly.
    """
    for body in re.findall(r"<!--(.*?)-->", raw, re.S):
        assert "--" not in body, (
            f"{path}: '--' inside an XML comment. Legal to ElementTree, fatal to "
            f"ClickHouse (exit 232 at boot). Offending comment: {body.strip()[:80]!r}"
        )
    return ET.fromstring(raw)

def load(path):
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
    cm  = next((d for d in docs if d["kind"] == "ConfigMap"), None)
    sts = next((d for d in docs if d["kind"] == "StatefulSet"), None)
    assert cm is not None,  f"{path}: no ClickHouse config ConfigMap rendered"
    assert sts is not None, f"{path}: no ClickHouse StatefulSet rendered"
    raw = cm["data"]["curie-logging.xml"]
    return cm, sts, raw, check_xml(raw, path)

# --- defaults -------------------------------------------------------------
_, sts, raw, root = load(sys.argv[1])

level = root.findtext("logger/level")
assert level == "warning", f"default logger level is {level!r}, expected 'warning'"

# The image logs only to files under /var/log/clickhouse-server/, so without
# this `kubectl logs` on the pod is empty and the diagnostics die with it.
assert root.findtext("logger/console") == "1", "logger/console must be on so kubectl logs works"

# text_log is KEPT and level-filtered, not removed: it is the queryable record
# that diagnosed the incident, and unlike the log file it outlives the pod.
tl_level = root.findtext("text_log/level")
assert tl_level == level, (
    f"text_log level is {tl_level!r} but the logger is {level!r}. The image ships "
    "<text_log><level>trace</level>, which is the actual firehose; it must be filtered."
)
assert root.find("text_log").get("remove") is None, "text_log must be kept, not removed"
tl_ttl = root.findtext("text_log/ttl")
assert tl_ttl and "DELETE" in tl_ttl, f"text_log kept without a deleting TTL: {tl_ttl!r}"

for t in SAMPLERS:
    el = root.find(t)
    assert el is not None, f"default render does not mention <{t}> at all"
    assert el.get("remove") == "1", f"<{t}> must be removed by default, got {el.attrib}"

for t in KEEP:
    ttl = root.findtext(f"{t}/ttl")
    assert ttl, f"<{t}> is kept but carries no TTL, it can grow unbounded"
    assert "DELETE" in ttl, f"<{t}> TTL does not delete: {ttl!r}"

# --- the StatefulSet actually consumes it (3, 4) --------------------------
tpl = sts["spec"]["template"]
# `or {}` not `.get(..., {})`: an emptied-out `annotations:` block parses as
# None, and `in None` raises TypeError instead of failing this assertion.
ann = tpl["metadata"].get("annotations") or {}
assert "checksum/config" in ann, "no checksum/config annotation: a config edit would not roll the pod"

vols = {v["name"] for v in tpl["spec"]["volumes"]}
assert "config" in vols, f"no `config` volume on the pod: {sorted(vols)}"

mounts = {m["name"]: m for m in tpl["spec"]["containers"][0]["volumeMounts"]}
assert "config" in mounts, "config volume is declared but never mounted"
cm_mount = mounts["config"]
assert cm_mount.get("subPath") == "curie-logging.xml", (
    "config mount must use subPath -- mounting the directory hides the image's "
    f"own docker_related_config.xml. got: {cm_mount}"
)
assert cm_mount["mountPath"].startswith("/etc/clickhouse-server/config.d/"), (
    f"config must land in config.d to be merged, got {cm_mount['mountPath']}"
)

# --- override keeps the samplers, but bounded (5) -------------------------
_, _, _, vroot = load(sys.argv[2])
vlevel = vroot.findtext("logger/level")
assert vlevel == "warning", f"override render changed the log level to {vlevel!r}"
for t in SAMPLERS:
    el = vroot.find(t)
    assert el is not None and el.get("remove") is None, (
        f"<{t}> should be present (not removed) when systemLogs.enabled=true"
    )
    ttl = vroot.findtext(f"{t}/ttl")
    assert ttl and "7 DAY" in ttl, (
        f"<{t}> must honour retentionDays even when enabled; got {ttl!r}"
    )
# retentionDays must reach text_log too, not just the opt-in samplers.
vtl = vroot.findtext("text_log/ttl")
assert vtl and "7 DAY" in vtl, f"text_log must honour retentionDays; got {vtl!r}"

print("  level+console, text_log filtered & kept, samplers removed, TTLs, mount + checksum, override: OK")
PY

# ------------------------------------------------------------------------ 7
python3 - "$NOPERSIST" <<'PY'
import sys, yaml
sts = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "StatefulSet")
vols = {v["name"] for v in sts["spec"]["template"]["spec"]["volumes"]}
assert vols == {"config", "data"}, (
    f"persistence=false must keep the `data` emptyDir alongside `config`, got {sorted(vols)}"
)
assert "volumeClaimTemplates" not in sts["spec"], "persistence=false must not render a PVC"
print("  persistence=false still gets its data emptyDir: OK")
PY

if [[ -d "$OFF" ]] && [[ -n "$(find "$OFF" -type f -path "*/templates/clickhouse.yaml" -print -quit)" ]]; then
  fail "clickhouse.deploy=false still rendered the ClickHouse manifest"
fi
echo "  clickhouse.deploy=false renders no ClickHouse manifest: OK"

# ------------------------------------------------------------------------ 9
# clickhouse.existingSecret must reach EVERY consumer of the password, not just
# the in-chart server. Structural check via PyYAML rather than grep: a
# line-oriented reader silently mis-reads a requoted value or a reordered key.
DEFAULT_TEMPLATES="$DEFAULT_TEMPLATES" \
EXISTING_SECRET_TEMPLATES="$EXISTING_SECRET_TEMPLATES" \
python3 <<'PY'
import os, sys, yaml

DEFAULT_TEMPLATES = os.environ["DEFAULT_TEMPLATES"]
BYO_TEMPLATES = os.environ["EXISTING_SECRET_TEMPLATES"]

# `helm template rel <chart>` -> fullname `rel-curie`.
CHART_SECRET_NAME = "rel-curie-secrets"
BYO_SECRET_NAME = "acme-ch"
KEY = "clickhousePassword"

failures = []


def containers(obj, acc):
    if isinstance(obj, dict):
        if isinstance(obj.get("containers"), list):
            acc.extend(obj["containers"])
        for v in obj.values():
            containers(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            containers(item, acc)


def check(aid, manifest, container, expected, ctx):
    if not os.path.isfile(manifest):
        failures.append(f"[{aid}] {ctx}: {manifest} did not render")
        return
    with open(manifest) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    found = []
    containers(docs, found)
    matched = [c for c in found if isinstance(c, dict) and c.get("name") == container]
    entries = [e for c in matched for e in (c.get("env") or [])
               if e.get("name") == "CLICKHOUSE_PASSWORD"]
    if len(entries) != 1:
        failures.append(f"[{aid}] {ctx}: CLICKHOUSE_PASSWORD rendered {len(entries)} times "
                        f"on container {container!r}, expected exactly 1")
        return
    ref = (entries[0].get("valueFrom") or {}).get("secretKeyRef")
    if not ref:
        failures.append(f"[{aid}] {ctx}: CLICKHOUSE_PASSWORD has no valueFrom.secretKeyRef "
                        "(an inline value would put the password in the manifest)")
        return
    if ref.get("name") != expected:
        failures.append(f"[{aid}] {ctx}: CLICKHOUSE_PASSWORD secretKeyRef.name = "
                        f"{ref.get('name')!r}, expected {expected!r}")
    if ref.get("key") != KEY:
        failures.append(f"[{aid}] {ctx}: CLICKHOUSE_PASSWORD secretKeyRef.key = "
                        f"{ref.get('key')!r}, expected {KEY!r}")


# 9a/9b: no-regression -- the default render still resolves to the chart Secret,
# so the escape does not repoint an install that never set existingSecret.
for i, c in enumerate(("langfuse-web", "langfuse-worker")):
    check(f"9a{i}", f"{DEFAULT_TEMPLATES}/langfuse.yaml", c, CHART_SECRET_NAME,
          f"default render, {c}")

# 9c/9d: both langfuse Deployments include the shared env helper separately, so
# a fix applied to one include site and not the other renders half a release.
for i, c in enumerate(("langfuse-web", "langfuse-worker")):
    check(f"9c{i}", f"{BYO_TEMPLATES}/langfuse.yaml", c, BYO_SECRET_NAME,
          f"clickhouse.existingSecret set, {c}")

# 9e: the in-chart server in the SAME render. With deploy=true + existingSecret,
# the server and Langfuse reading different Secrets is split-brain auth -- both
# sides are asserted together so a future edit cannot "fix" the split by
# breaking the server instead.
check("9e", f"{BYO_TEMPLATES}/clickhouse.yaml", "clickhouse", BYO_SECRET_NAME,
      "clickhouse.existingSecret set, in-chart StatefulSet")

if failures:
    for msg in failures:
        print(f"FAIL {msg}", file=sys.stderr)
    print(f"{len(failures)} of 5 assertion-9 checks failed. See #2052: Langfuse "
          "hardcoded the chart Secret while every other consumer honoured "
          "clickhouse.existingSecret, so trace ingestion died with the release green.",
          file=sys.stderr)
    sys.exit(1)

print("  clickhouse.existingSecret reaches langfuse web+worker and the StatefulSet, "
      "default unchanged: OK")
PY

echo
echo "PASS: ClickHouse self-telemetry defaults are bounded."
