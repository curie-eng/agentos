#!/usr/bin/env bash
#
# Structural assertions for the API Service NodePort contract (#1760):
#   (a) the default ClusterIP Service omits nodePort;
#   (b) switching to NodePort uses the pinned 30081 default;
#   (c) an explicit NodePort override wins;
#   (d) an empty NodePort value restores Kubernetes auto allocation by omitting
#       the field from the rendered Service.
set -euo pipefail

CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "FAIL [$1] $2" >&2; exit 1; }
render() { helm template curie "$CHART" "$@" 2>&1; }

assert_api_service() {
  local case_id="$1" expected_type="$2" expected_node_port="$3"
  shift 3

  local out
  if ! out="$(render -s templates/api.yaml "$@")"; then
    fail "$case_id" "helm template failed
$(head -3 <<<"$out")"
  fi

  CASE_ID="$case_id" python3 - "$out" "$expected_type" "$expected_node_port" <<'PY' || exit 1
import os
import sys

import yaml

case_id = os.environ["CASE_ID"]
documents = [doc for doc in yaml.safe_load_all(sys.argv[1]) if doc]
services = [
    doc
    for doc in documents
    if doc.get("kind") == "Service"
    and doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component") == "api"
]
if len(services) != 1:
    print(
        f"FAIL [{case_id}] expected exactly one API Service, found {len(services)}",
        file=sys.stderr,
    )
    sys.exit(1)

service = services[0]
expected_type = sys.argv[2]
actual_type = service.get("spec", {}).get("type")
if actual_type != expected_type:
    print(
        f"FAIL [{case_id}] Service type {actual_type!r}, expected {expected_type!r}",
        file=sys.stderr,
    )
    sys.exit(1)

ports = [port for port in service["spec"].get("ports", []) if port.get("name") == "http"]
if len(ports) != 1:
    print(
        f"FAIL [{case_id}] expected exactly one http port, found {len(ports)}",
        file=sys.stderr,
    )
    sys.exit(1)

port = ports[0]
expected_node_port = sys.argv[3]
if expected_node_port == "absent":
    if "nodePort" in port:
        print(
            f"FAIL [{case_id}] nodePort rendered unexpectedly: {port['nodePort']!r}",
            file=sys.stderr,
        )
        sys.exit(1)
else:
    expected = int(expected_node_port)
    actual = port.get("nodePort")
    if actual != expected:
        print(
            f"FAIL [{case_id}] nodePort {actual!r}, expected {expected}",
            file=sys.stderr,
        )
        sys.exit(1)
PY
}

# (a) A nonempty pinned default must never leak into the default ClusterIP.
assert_api_service a ClusterIP absent

# (b) NodePort without an override must use the stable published port.
assert_api_service b NodePort 30081 --set api.service.type=NodePort

# (c) Operators can choose another safe NodePort.
assert_api_service c NodePort 30181 \
  --set api.service.type=NodePort \
  --set api.service.nodePort=30181

# (d) An explicit empty string omits nodePort so Kubernetes allocates one.
assert_api_service d NodePort absent \
  --set api.service.type=NodePort \
  --set-string api.service.nodePort=

echo "api-service-nodeport-assertions: all four assertions passed"
