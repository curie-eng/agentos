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

TPL=templates/clickhouse.yaml

fail() { echo "FAIL: $*" >&2; exit 1; }

DEFAULT="$TMP/default.yaml"
VERBOSE="$TMP/verbose.yaml"
NOPERSIST="$TMP/nopersist.yaml"
OFF="$TMP/off.yaml"

echo "=== Rendering ClickHouse (defaults) ==="
helm template rel "$CHART" --show-only "$TPL" > "$DEFAULT"

echo "=== Rendering ClickHouse (systemLogs.enabled=true, retentionDays=7) ==="
helm template rel "$CHART" --show-only "$TPL" \
  --set clickhouse.systemLogs.enabled=true \
  --set clickhouse.systemLogs.retentionDays=7 > "$VERBOSE"

echo "=== Rendering ClickHouse (persistence disabled) ==="
helm template rel "$CHART" --show-only "$TPL" \
  --set clickhouse.persistence.enabled=false > "$NOPERSIST"

echo "=== Rendering ClickHouse (deploy=false) ==="
helm template rel "$CHART" --show-only "$TPL" \
  --set clickhouse.deploy=false > "$OFF" 2>/dev/null || true

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

if grep -q "kind:" "$OFF" 2>/dev/null; then
  fail "clickhouse.deploy=false still rendered objects"
fi
echo "  clickhouse.deploy=false renders nothing: OK"

echo
echo "PASS: ClickHouse self-telemetry defaults are bounded."
