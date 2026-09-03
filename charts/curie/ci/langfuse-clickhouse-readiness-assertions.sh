#!/usr/bin/env bash
#
# Regression test for issue #2009 (Langfuse web restarts during a Helm upgrade
# while the ClickHouse Service is unavailable).
#
# Observed on a live in-place `next` upgrade: the Helm revision completed, but
# the new Langfuse web pod restarted three times because it began its ClickHouse
# migrations before the recreated chart-owned ClickHouse Service was resolvable:
#
#   failed to open database: dial tcp: lookup curie-clickhouse on 10.43.0.10:53: no such host
#
# Kubernetes reported BackOff and the release converged only through
# CrashLoopBackOff retries. The fix is the `wait-for-clickhouse` init container
# both Langfuse deployments now render (helper `curie.langfuse.clickhouseGate`),
# which polls ClickHouse's HTTP `/ping` until it answers 200 -- so the
# application container is not started at all until the dependency is up.
#
# This is deliberately NOT a render-shape-only test. A rendered init container
# proves nothing about whether the gate actually waits, so the behavioural
# assertions EXTRACT the rendered shell script and RUN it against a stub that
# reproduces the delayed dependency:
#
#   1. Delayed ClickHouse: the endpoint is closed when the gate starts and only
#      opens several seconds later. The gate must WAIT and then exit 0 in a
#      single invocation -- the zero-restart property the issue asks for.
#   2. Never-ready ClickHouse: the endpoint never opens. The gate must exit
#      NON-ZERO after its bounded attempts. Without this, assertion 1 would pass
#      for a gate that returns success unconditionally.
#   3. Reachable but refusing: the endpoint answers 503. The gate must still not
#      pass, proving it checks that ClickHouse is *accepting* queries rather
#      than only that the name resolves.
#   4. Slow but healthy: the endpoint takes 3s to answer 200. The gate must pass
#      once `clickhouseReadiness.timeoutSeconds` allows for it and fail while it
#      does not -- the per-request timeout is a probe setting, so it lives in
#      values rather than the template (charts/curie/CLAUDE.md's probe-settings
#      invariant) and this proves the value is actually honoured.
#
# Plus render assertions that the gate is wired into BOTH Langfuse deployments
# (web and worker are the same boot path -- the sibling of the reported one),
# probes the same endpoint the application is configured with, carries the same
# securityContext as its application container (the #351 named-image-user class
# applies to init containers too), and is operator-disableable.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
cleanup() {
  [[ -n "${STUB_PID:-}" ]] && kill "$STUB_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v node >/dev/null 2>&1 || fail "node is required: the rendered gate probes ClickHouse with the Langfuse images' own node runtime, and these assertions execute that rendered script"

DEFAULT="$TMP/default.yaml"
PATIENT="$TMP/patient.yaml"
FAST="$TMP/fast.yaml"
TOLERANT="$TMP/tolerant.yaml"
DISABLED="$TMP/disabled.yaml"

echo "=== Rendering templates/langfuse.yaml (defaults) ==="
helm template rel "$CHART" --show-only templates/langfuse.yaml > "$DEFAULT"

echo "=== Rendering templates/langfuse.yaml (1s polls, room to wait) ==="
helm template rel "$CHART" --show-only templates/langfuse.yaml \
  --set langfuse.clickhouseReadiness.maxAttempts=30 \
  --set langfuse.clickhouseReadiness.intervalSeconds=1 > "$PATIENT"

echo "=== Rendering templates/langfuse.yaml (short readiness bounds) ==="
helm template rel "$CHART" --show-only templates/langfuse.yaml \
  --set langfuse.clickhouseReadiness.maxAttempts=3 \
  --set langfuse.clickhouseReadiness.intervalSeconds=1 > "$FAST"

echo "=== Rendering templates/langfuse.yaml (short bounds, generous probe timeout) ==="
helm template rel "$CHART" --show-only templates/langfuse.yaml \
  --set langfuse.clickhouseReadiness.maxAttempts=3 \
  --set langfuse.clickhouseReadiness.intervalSeconds=1 \
  --set langfuse.clickhouseReadiness.timeoutSeconds=10 > "$TOLERANT"

echo "=== Rendering templates/langfuse.yaml (gate disabled) ==="
helm template rel "$CHART" --show-only templates/langfuse.yaml \
  --set langfuse.clickhouseReadiness.enabled=false > "$DISABLED"

# ---------------------------------------------------------------- render shape

cat > "$TMP/shape.py" <<'PY'
import sys, yaml

path, disabled_path = sys.argv[1], sys.argv[2]


def deployments(path):
    out = {}
    for doc in yaml.safe_load_all(open(path)):
        if doc and doc.get("kind") == "Deployment":
            out[doc["metadata"]["name"]] = doc
    return out


def podspec(dep):
    return dep["spec"]["template"]["spec"]


def env_of(container, name):
    for entry in container.get("env") or []:
        if entry.get("name") == name:
            return entry.get("value")
    return None


rendered = deployments(path)
for suffix, app_name in (("-langfuse-web", "langfuse-web"), ("-langfuse-worker", "langfuse-worker")):
    dep = next((d for n, d in rendered.items() if n.endswith(suffix)), None)
    if dep is None:
        raise SystemExit(f"no Deployment ending in {suffix!r} rendered")
    spec = podspec(dep)
    inits = spec.get("initContainers") or []
    gate = next((c for c in inits if c.get("name") == "wait-for-clickhouse"), None)
    if gate is None:
        raise SystemExit(
            f"{suffix}: no 'wait-for-clickhouse' init container -- the application "
            f"container can start before ClickHouse resolves, which is the #2009 regression "
            f"(got initContainers {[c.get('name') for c in inits]})"
        )

    app = next((c for c in spec["containers"] if c.get("name") == app_name), None)
    if app is None:
        raise SystemExit(f"{suffix}: no {app_name!r} application container rendered")
    if app_name in {c.get("name") for c in inits}:
        raise SystemExit(f"{suffix}: the application container must not be an init container")

    # The gate must probe exactly the endpoint the application is configured to
    # use, or it gates on the wrong host and the bug survives.
    gate_url = env_of(gate, "CLICKHOUSE_URL")
    app_url = env_of(app, "CLICKHOUSE_URL")
    if not gate_url:
        raise SystemExit(f"{suffix}: gate has no CLICKHOUSE_URL env")
    if gate_url != app_url:
        raise SystemExit(
            f"{suffix}: gate probes {gate_url!r} but the application connects to {app_url!r}"
        )

    # #351 class: a named image user plus runAsNonRoot is CreateContainerConfigError.
    if (gate.get("securityContext") or {}) != (app.get("securityContext") or {}):
        raise SystemExit(
            f"{suffix}: gate securityContext {gate.get('securityContext')!r} does not match the "
            f"application container's {app.get('securityContext')!r}; the init container is subject "
            f"to the same numeric-runAsUser requirement (#351)"
        )

    print(f"  ok: {suffix.lstrip('-')} gates on wait-for-clickhouse probing {gate_url}")

for name, dep in deployments(disabled_path).items():
    for container in podspec(dep).get("initContainers") or []:
        if container.get("name") == "wait-for-clickhouse":
            raise SystemExit(f"{name}: gate still rendered with clickhouseReadiness.enabled=false")
print("  ok: clickhouseReadiness.enabled=false removes the gate from both deployments")
PY

echo "=== Assertion 1: both Langfuse deployments render the ClickHouse gate ==="
if ! out="$(python3 "$TMP/shape.py" "$DEFAULT" "$DISABLED" 2>&1)"; then
  fail "$out"
fi
echo "$out"

# ------------------------------------------------------------------ behaviour

# Extract the rendered gate script so the assertions below execute exactly what
# the chart ships, not a paraphrase of it.
cat > "$TMP/extract.py" <<'PY'
import sys, yaml

for doc in yaml.safe_load_all(open(sys.argv[1])):
    if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-langfuse-web"):
        for container in doc["spec"]["template"]["spec"].get("initContainers") or []:
            if container["name"] == "wait-for-clickhouse":
                sys.stdout.write(container["args"][0])
                raise SystemExit(0)
raise SystemExit("could not extract the wait-for-clickhouse script from the rendered web Deployment")
PY

python3 "$TMP/extract.py" "$PATIENT" > "$TMP/gate-patient.sh"
python3 "$TMP/extract.py" "$FAST" > "$TMP/gate-fast.sh"
python3 "$TMP/extract.py" "$TOLERANT" > "$TMP/gate-tolerant.sh"
[[ -s "$TMP/gate-patient.sh" && -s "$TMP/gate-fast.sh" && -s "$TMP/gate-tolerant.sh" ]] || fail "an extracted gate script is empty"

# A free port that nothing is listening on: the delayed/absent ClickHouse.
# There is an unavoidable gap between choosing the port here and the stub taking
# it, so the stub binds immediately and delays only its listen() (see stub.py).
# That shrinks the window from the length of the delay to one interpreter start.
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"

# Stub ClickHouse. $1 = seconds to stay down before binding, $2 = /ping status,
# $3 = port, $4 = seconds each /ping response is held back (slow but healthy).
cat > "$TMP/stub.py" <<'PY'
import sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

delay, status, port = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
response_delay = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        time.sleep(response_delay)
        self.send_response(status)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"Ok.\n")

    def log_message(self, *args):
        pass

    # A probe that times out hangs up mid-response; that is the behaviour under
    # test, not a stub failure, so it must not print a traceback.
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


# Hold the port for the whole delay, do not leave it free and re-take it later.
# `bind_and_activate=False` lets us bind now and listen() only after the delay.
# A bound-but-not-listening socket refuses connections exactly as an unbound
# port does, so the gate still sees the dependency as DOWN -- which is what
# assertion 2 is for -- while no other process on the machine can be handed the
# port in the meantime.
#
# Releasing it was the flake: the caller picks the port with bind(0) + close(),
# and binding here only after `time.sleep(delay)` left that ephemeral port free
# for the full delay (4s in assertion 2). On a busy runner something else can
# take it in that window and answer the probe, which is what "ClickHouse ready
# after 1 attempt(s)" against a supposedly-down dependency means.
server = HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
server.server_bind()
time.sleep(delay)
server.server_activate()
server.serve_forever()
PY

run_gate() {  # run_gate <gate-script> <outfile>; returns the gate's exit status
  set +e
  CLICKHOUSE_URL="http://127.0.0.1:$PORT" sh "$1" > "$2" 2>&1
  local status=$?
  set -e
  return $status
}

echo
echo "=== Assertion 2: a DELAYED ClickHouse is waited for, not crash-looped ==="
python3 "$TMP/stub.py" 4 200 "$PORT" & STUB_PID=$!
started="$(date +%s)"
if ! run_gate "$TMP/gate-patient.sh" "$TMP/delayed.log"; then
  echo "--- gate output ---"; cat "$TMP/delayed.log"
  fail "the gate exited non-zero against a ClickHouse that came up after 4s; a routine upgrade would still CrashLoopBackOff (#2009)"
fi
elapsed=$(( $(date +%s) - started ))
kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; STUB_PID=""
grep -q "Waiting for ClickHouse readiness" "$TMP/delayed.log" \
  || fail "the gate did not report waiting; it cannot have observed the dependency being down (output: $(cat "$TMP/delayed.log"))"
grep -q "exiting for init container restart" "$TMP/delayed.log" \
  && fail "the gate gave up and asked for a restart instead of waiting out a 4s ClickHouse delay"
(( elapsed >= 3 )) \
  || fail "the gate returned after ${elapsed}s against a ClickHouse that was down for 4s -- it cannot have probed the real endpoint"
echo "  ok: gate waited ${elapsed}s for the delayed dependency, then exited 0 in a single run (zero restarts)"

echo
echo "=== Assertion 3: a ClickHouse that never comes up fails the gate (bounded) ==="
if run_gate "$TMP/gate-fast.sh" "$TMP/absent.log"; then
  echo "--- gate output ---"; cat "$TMP/absent.log"
  fail "the gate exited 0 with NOTHING listening -- it passes vacuously, so assertion 2 proves nothing"
fi
grep -q "ClickHouse unreachable at" "$TMP/absent.log" \
  || fail "the gate failed without naming the unreachable endpoint (output: $(cat "$TMP/absent.log"))"
echo "  ok: gate exits non-zero after its bounded attempts and names the endpoint"

echo
echo "=== Assertion 4: reachable but not serving (HTTP 503) does not open the gate ==="
python3 "$TMP/stub.py" 0 503 "$PORT" & STUB_PID=$!
sleep 1
if run_gate "$TMP/gate-fast.sh" "$TMP/refusing.log"; then
  echo "--- gate output ---"; cat "$TMP/refusing.log"
  fail "the gate passed against a ClickHouse answering 503; it gates on name resolution only, not on ClickHouse accepting queries"
fi
kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; STUB_PID=""
echo "  ok: a non-200 /ping keeps the gate closed"

echo
echo "=== Assertion 5: the probe timeout is a values knob, and it is honoured ==="
python3 "$TMP/stub.py" 0 200 "$PORT" 3 & STUB_PID=$!
sleep 1
if run_gate "$TMP/gate-fast.sh" "$TMP/slow-strict.log"; then
  echo "--- gate output ---"; cat "$TMP/slow-strict.log"
  fail "the gate passed against a /ping taking 3s while timeoutSeconds was 2 -- the rendered per-request timeout is not being applied"
fi
if ! run_gate "$TMP/gate-tolerant.sh" "$TMP/slow-tolerant.log"; then
  echo "--- gate output ---"; cat "$TMP/slow-tolerant.log"
  fail "the gate still failed against a healthy 3s /ping with timeoutSeconds=10; the operator knob does not widen the probe, so a slow BYO ClickHouse can never open the gate"
fi
kill "$STUB_PID" 2>/dev/null || true; wait "$STUB_PID" 2>/dev/null || true; STUB_PID=""
echo "  ok: a 3s /ping fails at timeoutSeconds=2 and passes at timeoutSeconds=10"

echo
echo "PASS: both Langfuse deployments gate startup on ClickHouse's HTTP /ping; the rendered gate waits out a delayed dependency and exits 0 once, fails bounded when ClickHouse never appears, and stays closed while ClickHouse answers non-200, and honours the operator's probe timeout (issue #2009)."
