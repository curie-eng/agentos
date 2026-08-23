#!/usr/bin/env bash
# Render contract for managed workspaces and approval-gated publication.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RENDERED="$TMP/rendered.yaml"
SEALED="$TMP/sealed.yaml"
helm template remote-dev "$CHART" -f "$CHART/values-dev.yaml" \
  --set agentSandbox.deploy=true \
  --set agentSandbox.controller.deploy=false > "$RENDERED"
helm template remote-dev "$CHART" --show-only templates/secrets.yaml > "$SEALED"

python3 - "$RENDERED" "$SEALED" "$CHART/values.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

rendered_path, sealed_path, values_path = map(Path, sys.argv[1:])
docs = [doc for doc in yaml.safe_load_all(rendered_path.read_text()) if doc]
sealed_docs = [doc for doc in yaml.safe_load_all(sealed_path.read_text()) if doc]
values = yaml.safe_load(values_path.read_text())


def fail(message):
    raise AssertionError(message)


def one(kind, *, component=None, name_suffix=None):
    matches = []
    for doc in docs:
        if doc.get("kind") != kind:
            continue
        metadata = doc.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if component is not None and labels.get("app.kubernetes.io/component") != component:
            continue
        if name_suffix is not None and not str(metadata.get("name", "")).endswith(name_suffix):
            continue
        matches.append(doc)
    if len(matches) != 1:
        fail(f"expected one {kind} component={component!r} suffix={name_suffix!r}, got {len(matches)}")
    return matches[0]


def containers(workload):
    if workload["kind"] == "Deployment":
        return workload["spec"]["template"]["spec"]["containers"]
    return workload["spec"]["podTemplate"]["spec"]["containers"]


def env_map(container):
    return {entry["name"]: entry for entry in container.get("env", [])}


# Dedicated worker auth is generated independently and mounted only into the
# API and worker. It is neither the platform CLI key nor sandbox state.
secret = one("Secret", name_suffix="-secrets")
string_data = secret.get("stringData") or {}
worker_auth = string_data.get("internalWorkerToken")
api_key = string_data.get("apiKey")
if not worker_auth:
    fail("development Secret must carry internalWorkerToken")
if worker_auth == api_key:
    fail("internalWorkerToken must not equal the platform apiKey")
sealed_secret = next(doc for doc in sealed_docs if doc.get("kind") == "Secret")
sealed_worker_auth = (sealed_secret.get("stringData") or {}).get("internalWorkerToken")
if not sealed_worker_auth or len(sealed_worker_auth) < 32:
    fail("sealed internalWorkerToken must be a generated strong value")
if sealed_worker_auth == (sealed_secret.get("stringData") or {}).get("apiKey"):
    fail("sealed internalWorkerToken must not equal the platform apiKey")

api = one("Deployment", component="api")
worker = one("Deployment", component="worker")
for workload in (api, worker):
    env = env_map(containers(workload)[0])
    ref = env.get("CURIE_INTERNAL_WORKER_TOKEN", {}).get("valueFrom", {}).get("secretKeyRef", {})
    if ref.get("key") != "internalWorkerToken":
        fail(f"{workload['metadata']['name']} must mount internalWorkerToken")

for workload in [doc for doc in docs if doc.get("kind") == "Deployment" and doc not in (api, worker)]:
    for container in containers(workload):
        if "CURIE_INTERNAL_WORKER_TOKEN" in env_map(container):
            fail(f"internal worker auth leaked into {workload['metadata']['name']}")


# The trusted worker gets exactly the publication resource verbs and no
# pods/exec. A dynamic Job can be created and observed; a sandbox cannot be
# entered through the worker's service account.
role = one("Role", component="worker")
rules = role.get("rules") or []
resources_to_verbs = {}
for rule in rules:
    for resource in rule.get("resources") or []:
        resources_to_verbs.setdefault(resource, set()).update(rule.get("verbs") or [])
for resource in ("jobs", "configmaps", "secrets"):
    if not {"create", "get", "delete"} <= resources_to_verbs.get(resource, set()):
        fail(f"worker Role is missing publication lifecycle verbs for {resource}")
if "get" not in resources_to_verbs.get("pods/log", set()):
    fail("worker Role cannot read the publication Job URL marker")
if "pods/exec" in resources_to_verbs:
    fail("worker Role must never grant pods/exec")


# Publication Jobs use a no-RBAC, tokenless identity. No RoleBinding may name
# it; the Job itself is built dynamically and separately unit-tested.
publication_sa = one("ServiceAccount", component="publication")
if publication_sa.get("automountServiceAccountToken") is not False:
    fail("publication ServiceAccount must disable token automount")
publication_sa_name = publication_sa["metadata"]["name"]
for binding in [doc for doc in docs if doc.get("kind") in ("RoleBinding", "ClusterRoleBinding")]:
    if any(subject.get("name") == publication_sa_name for subject in binding.get("subjects") or []):
        fail("publication ServiceAccount must have no RBAC binding")
one("ConfigMap", component="publication-owner")


# Worker scratch is private and bounded; worker resources include explicit
# memory/CPU/ephemeral limits for clone/archive work.
worker_pod = worker["spec"]["template"]["spec"]
clone_volume = next((v for v in worker_pod.get("volumes", []) if v.get("name") == "workspace-clone"), None)
if clone_volume is None or clone_volume.get("emptyDir", {}).get("sizeLimit") != "4Gi":
    fail("worker workspace-clone emptyDir must be bounded to 4Gi")
worker_container = containers(worker)[0]
if not any(m.get("name") == "workspace-clone" for m in worker_container.get("volumeMounts", [])):
    fail("worker must mount the workspace-clone volume")
expected_worker_resources = {
    "requests": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "2Gi"},
    "limits": {"cpu": "2", "memory": "1Gi", "ephemeral-storage": "8Gi"},
}
if worker_container.get("resources") != expected_worker_resources:
    fail(f"worker clone resources differ: {worker_container.get('resources')!r}")


# Sandbox receives only claim-scoped workspace facts. Both init stages and the
# runner share a dedicated 1Gi /workspace; no object-store identity survives in
# any sandbox container and /workspace is not a general writable-root path.
sandbox = one("SandboxTemplate", component="agent-sandbox")
pod = sandbox["spec"]["podTemplate"]["spec"]
workspace_volume = next((v for v in pod.get("volumes", []) if v.get("name") == "workspace"), None)
if workspace_volume is None or workspace_volume.get("emptyDir", {}).get("sizeLimit") != "1Gi":
    fail("sandbox workspace emptyDir must be bounded to 1Gi")
init_containers = list(pod.get("initContainers") or [])
runner = next((c for c in pod.get("containers") or [] if c.get("name") == "runner"), None)
if runner is None:
    fail("sandbox is missing runner")
workspace_inits = [
    container for container in init_containers
    if any(m.get("name") == "workspace" and m.get("mountPath") == "/workspace"
           for m in container.get("volumeMounts", []))
]
if len(workspace_inits) < 2:
    fail("sandbox fetch and extract stages must both share /workspace")
if not any("fetch" in container.get("name", "") for container in workspace_inits):
    fail("no workspace-mounted fetch init stage")
if not any("extract" in container.get("name", "") for container in workspace_inits):
    fail("no workspace-mounted extract init stage")
if not any(m.get("name") == "workspace" and m.get("mountPath") == "/workspace"
           for m in runner.get("volumeMounts", [])):
    fail("runner must share workspace at /workspace")

all_containers = init_containers + list(pod.get("containers") or [])

for container in all_containers:
    names = set(env_map(container))
    forbidden = {
        "S3_ACCESS_KEY", "S3_SECRET_KEY", "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "CURIE_INTERNAL_WORKER_TOKEN",
    }
    leaked = names & forbidden
    if leaked:
        fail(f"sandbox container {container.get('name')} receives credential env {sorted(leaked)}")

writable = values["agentSandbox"]["runner"]["hardening"]["writablePaths"]
paths = [item["path"] if isinstance(item, dict) else item for item in writable]
if "/workspace" in paths:
    fail("/workspace must be a dedicated bounded volume, not hardening.writablePaths")


# Every runner NetworkPolicy selects only runner-sandbox labels; the dynamic
# publication component therefore remains outside the fail-closed sandbox
# selector and needs no GitHub widening on sandbox pods.
policies = [doc for doc in docs if doc.get("kind") == "NetworkPolicy" and "runner" in doc["metadata"]["name"]]
if not policies:
    fail("runner fail-closed NetworkPolicies are absent")
for policy in policies:
    labels = policy.get("spec", {}).get("podSelector", {}).get("matchLabels", {})
    if labels.get("app.kubernetes.io/component") != "runner-sandbox":
        fail(f"runner policy selector widened: {policy['metadata']['name']}")
    if labels.get("app.kubernetes.io/component") == "publication":
        fail("publication pods were selected by sandbox NetworkPolicy")

print("remote-dev capability render assertions passed")
PY
