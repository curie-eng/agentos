"""Deterministic, secret-minimizing Kubernetes publication resources.

The patch is carried as ConfigMap ``binaryData`` and the short-lived GitHub
credential lives only in a Secret volume.  Neither value appears in Job argv,
environment, labels, or logs.  Names are publication-id-derived so a worker
restart adopts the same resource set instead of starting a second push.
"""

from __future__ import annotations

import base64
import copy
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

if TYPE_CHECKING:
    from .publication_loop import PublicationJobObservation

MAX_PATCH_BYTES = 900_000


class PublicationResourceError(RuntimeError):
    """A publication cannot be represented by the hardened Job contract."""


@dataclass(frozen=True)
class PublicationJobSettings:
    namespace: str
    runner_image: str
    image_pull_policy: str
    image_pull_secrets: tuple[str, ...]
    priority_class_name: str
    service_account_name: str
    owner_name: str
    git_user_name: str
    git_user_email: str
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    ephemeral_request: str
    ephemeral_limit: str
    owner_uid: str | None = None


@dataclass(frozen=True)
class PublicationPayload:
    publication_id: uuid.UUID
    repo_full_name: str
    clean_clone_url: str
    base_sha: str
    patch: bytes
    branch: str
    title: str
    body: str


@dataclass(frozen=True)
class PublicationResourceNames:
    config_map: str
    secret: str
    job: str


@dataclass(frozen=True)
class PublicationResources:
    names: PublicationResourceNames
    config_map: dict[str, Any]
    secret: dict[str, Any]
    job: dict[str, Any]
    owner_uid: str


def deterministic_publication_branch(publication_id: uuid.UUID) -> str:
    return f"curie/publication-{publication_id.hex}"


def _resource_names(publication_id: uuid.UUID) -> PublicationResourceNames:
    suffix = publication_id.hex[:20]
    return PublicationResourceNames(
        config_map=f"curie-publication-{suffix}",
        secret=f"curie-publication-{suffix}",
        job=f"curie-publication-{suffix}",
    )


_PUBLISH_SCRIPT = r"""#!/bin/bash
set -euo pipefail
umask 077

redact() {
  # Credentials are never deliberately logged. This filter is defence in depth
  # for diagnostics returned by git or GitHub.
  sed -e 's#https://[^/@[:space:]]*@github\.com#https://github.com#g' \
      -e 's#Authorization: [^[:space:]]*#Authorization: [REDACTED]#g'
}

cleanup_auth() {
  rm -f /tmp/curie-git-user /tmp/curie-git-pass /tmp/curie-askpass
}
trap cleanup_auth EXIT

python - <<'PY'
import base64
from pathlib import Path

value = Path("/credentials/credential").read_text().strip()
user, password = "x-access-token", value
if value.lower().startswith("basic "):
    try:
        decoded = base64.b64decode(value.split(None, 1)[1]).decode()
        user, password = decoded.split(":", 1)
    except (ValueError, UnicodeError):
        raise SystemExit("publication credential is not valid Basic authorization")
elif value.lower().startswith(("bearer ", "token ")):
    password = value.split(None, 1)[1]
Path("/tmp/curie-git-user").write_text(user)
Path("/tmp/curie-git-pass").write_text(password)
PY

cat >/tmp/curie-askpass <<'ASKPASS'
#!/bin/sh
case "$1" in
  *sername*) cat /tmp/curie-git-user ;;
  *) cat /tmp/curie-git-pass ;;
esac
ASKPASS
chmod 0700 /tmp/curie-askpass
export GIT_ASKPASS=/tmp/curie-askpass
export GIT_TERMINAL_PROMPT=0

mkdir -p /work
cd /work
git clone "$CLEAN_CLONE_URL" repo 2> >(redact >&2)
cd repo
git remote set-url origin "$CLEAN_CLONE_URL"
git config --get remote.origin.url | grep -Fx "$CLEAN_CLONE_URL" >/dev/null
if ! git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
  git fetch --depth=1 origin "$BASE_SHA" 2> >(redact >&2)
fi
git checkout --detach "$BASE_SHA" 2> >(redact >&2)
git switch -c "$BRANCH" 2> >(redact >&2)
git apply --check --binary /publication/changes.patch
git apply --binary /publication/changes.patch
git add --all
if git diff --cached --quiet; then
  echo "publication patch produced no changes" >&2
  exit 1
fi
git -c user.name="$GIT_USER_NAME" -c user.email="$GIT_USER_EMAIL" commit -m "$PR_TITLE"
git push origin "HEAD:refs/heads/$BRANCH" 2> >(redact >&2)

python - <<'PY'
import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

repo = os.environ["REPO_FULL_NAME"]
branch = os.environ["BRANCH"]
owner = repo.split("/", 1)[0]
repo_api = f"https://api.github.com/repos/{repo}"
api = f"{repo_api}/pulls"
raw = Path("/credentials/credential").read_text().strip()
if raw.lower().startswith(("basic ", "bearer ", "token ")):
    authorization = raw
else:
    authorization = f"Bearer {raw}"
headers = {
    "Accept": "application/vnd.github+json",
    "Authorization": authorization,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "curie-publication-job",
}

def request(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=30) as response:
        return json.load(response)

def existing():
    head=quote(f"{owner}:{branch}", safe="")
    rows = request("GET", f"{api}?state=open&head={head}")
    return rows[0].get("html_url") if rows else None

# Query the deterministic head before POST, and again after an ambiguous REST
# failure. This is the idempotency boundary for a lost response.
url = existing()
if not url:
    repository = request("GET", repo_api)
    base = repository.get("default_branch")
    if not isinstance(base, str) or not base:
        raise SystemExit("GitHub repository has no default branch")
    try:
        created = request("POST", api, {
            "title": os.environ["PR_TITLE"],
            "head": branch,
            "base": base,
            "body": os.environ["PR_BODY"],
        })
        url = created.get("html_url")
    except (HTTPError, URLError):
        url = existing()
        if not url:
            raise
if not isinstance(url, str) or not url.startswith("https://github.com/"):
    raise SystemExit("GitHub did not return a usable pull request URL")
print(f"CURIE_PR_URL={url}")
PY
"""


def _owner_reference(settings: PublicationJobSettings, owner_uid: str) -> list[dict[str, Any]]:
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "name": settings.owner_name,
            "uid": owner_uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }
    ]


def build_publication_resources(
    payload: PublicationPayload,
    *,
    credential: str,
    settings: PublicationJobSettings,
) -> PublicationResources:
    if len(payload.patch) > MAX_PATCH_BYTES:
        raise PublicationResourceError(
            f"publication patch exceeds the {MAX_PATCH_BYTES} raw-byte limit"
        )
    if payload.branch != deterministic_publication_branch(payload.publication_id):
        raise PublicationResourceError("publication branch is not deterministic")
    if not credential.strip():
        raise PublicationResourceError("publication credential is empty")
    names = _resource_names(payload.publication_id)
    owner_uid = settings.owner_uid or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"curie:{settings.namespace}:{settings.owner_name}")
    )
    labels = {
        "app.kubernetes.io/managed-by": "curie",
        "curietech.ai/component": "publication",
        "curietech.ai/publication-id": str(payload.publication_id),
    }
    metadata: dict[str, Any] = {
        "namespace": settings.namespace,
        "labels": labels,
        "ownerReferences": _owner_reference(settings, owner_uid),
    }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {**metadata, "name": names.config_map},
        "immutable": True,
        "binaryData": {
            "changes.patch": base64.b64encode(payload.patch).decode("ascii"),
        },
        "data": {"publish.sh": _PUBLISH_SCRIPT},
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {**metadata, "name": names.secret},
        "immutable": True,
        "type": "Opaque",
        "stringData": {"credential": credential},
    }
    env = [
        {"name": "REPO_FULL_NAME", "value": payload.repo_full_name},
        {"name": "CLEAN_CLONE_URL", "value": payload.clean_clone_url},
        {"name": "BASE_SHA", "value": payload.base_sha},
        {"name": "BRANCH", "value": payload.branch},
        {"name": "PR_TITLE", "value": payload.title},
        {"name": "PR_BODY", "value": payload.body},
        {"name": "GIT_USER_NAME", "value": settings.git_user_name},
        {"name": "GIT_USER_EMAIL", "value": settings.git_user_email},
    ]
    pod_spec: dict[str, Any] = {
        "serviceAccountName": settings.service_account_name,
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "priorityClassName": settings.priority_class_name,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "imagePullSecrets": [{"name": name} for name in settings.image_pull_secrets],
        "containers": [
            {
                "name": "publish",
                "image": settings.runner_image,
                "imagePullPolicy": settings.image_pull_policy,
                "command": ["/bin/bash", "/publication/publish.sh"],
                "env": env,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "resources": {
                    "requests": {
                        "cpu": settings.cpu_request,
                        "memory": settings.memory_request,
                        "ephemeral-storage": settings.ephemeral_request,
                    },
                    "limits": {
                        "cpu": settings.cpu_limit,
                        "memory": settings.memory_limit,
                        "ephemeral-storage": settings.ephemeral_limit,
                    },
                },
                "volumeMounts": [
                    {"name": "publication", "mountPath": "/publication", "readOnly": True},
                    {"name": "credentials", "mountPath": "/credentials", "readOnly": True},
                    {"name": "work", "mountPath": "/work"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "publication", "configMap": {"name": names.config_map}},
            {"name": "credentials", "secret": {"secretName": names.secret}},
            {"name": "work", "emptyDir": {"sizeLimit": "2Gi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}},
        ],
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {**metadata, "name": names.job},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": dict(labels)},
                "spec": pod_spec,
            },
        },
    }
    return PublicationResources(
        names=names,
        config_map=config_map,
        secret=secret,
        job=job,
        owner_uid=owner_uid,
    )


class KubernetesPublicationCluster:
    """Create-or-adopt the deterministic resource set on a real apiserver."""

    def __init__(self, namespace: str, *, kubeconfig: str | None = None) -> None:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(config_file=kubeconfig)
        self.namespace = namespace
        self._core = k8s_client.CoreV1Api()
        self._batch = k8s_client.BatchV1Api()

    def owner_uid(self, owner_name: str) -> str:
        owner = self._core.read_namespaced_config_map(owner_name, self.namespace)
        uid = getattr(getattr(owner, "metadata", None), "uid", None)
        if not uid:
            raise PublicationResourceError(f"publication owner ConfigMap {owner_name!r} has no UID")
        return str(uid)

    @staticmethod
    def _is_not_found(exc: k8s_client.ApiException) -> bool:
        return bool(exc.status == 404)

    def _create_or_adopt(self, kind: str, obj: dict[str, Any]) -> None:
        name = str(obj["metadata"]["name"])
        expected_id = obj["metadata"]["labels"]["curietech.ai/publication-id"]
        api = self._batch if kind == "Job" else self._core
        suffix = {"ConfigMap": "config_map", "Secret": "secret", "Job": "job"}[kind]
        try:
            existing = getattr(api, f"read_namespaced_{suffix}")(name, self.namespace)
        except k8s_client.ApiException as exc:
            if not self._is_not_found(exc):
                raise
            getattr(api, f"create_namespaced_{suffix}")(self.namespace, body=obj)
            return
        labels = getattr(getattr(existing, "metadata", None), "labels", None) or {}
        if labels.get("curietech.ai/publication-id") != expected_id:
            raise PublicationResourceError(
                f"refusing to adopt {kind} {name!r} owned by another publication"
            )

    def apply(self, resources: PublicationResources) -> None:
        # Replace the builder's deterministic test UID with the live owner UID
        # before anything reaches the apiserver. The owner object is Helm-owned,
        # so uninstall garbage-collects a worker crash's leftovers.
        owner_name = resources.config_map["metadata"]["ownerReferences"][0]["name"]
        live_uid = self.owner_uid(str(owner_name))
        objects = [
            copy.deepcopy(resources.config_map),
            copy.deepcopy(resources.secret),
            copy.deepcopy(resources.job),
        ]
        for obj in objects:
            obj["metadata"]["ownerReferences"][0]["uid"] = live_uid
        for obj in objects:
            self._create_or_adopt(str(obj["kind"]), obj)

    def observe(self, job_name: str) -> PublicationJobObservation:
        # Local import avoids a module import cycle: publication_loop owns the
        # neutral observation DTO while this module owns Kubernetes shapes.
        from .publication_loop import PublicationJobObservation

        try:
            job = self._batch.read_namespaced_job(job_name, self.namespace)
        except k8s_client.ApiException as exc:
            if self._is_not_found(exc):
                return PublicationJobObservation(phase="pending", pr_url=None, logs="", error=None)
            raise
        status = getattr(job, "status", None)
        phase = "running"
        error: str | None = None
        if getattr(status, "succeeded", 0):
            phase = "succeeded"
        elif getattr(status, "failed", 0):
            phase = "failed"
            conditions = getattr(status, "conditions", None) or []
            error = (
                "; ".join(
                    str(getattr(condition, "message", "") or getattr(condition, "reason", ""))
                    for condition in conditions
                    if getattr(condition, "status", None) == "True"
                )
                or "publication Job failed"
            )

        logs = ""
        try:
            pods = self._core.list_namespaced_pod(
                self.namespace, label_selector=f"job-name={job_name}"
            )
            items = getattr(pods, "items", None) or []
            if items:
                pod_name = str(items[0].metadata.name)
                logs = str(
                    self._core.read_namespaced_pod_log(
                        pod_name,
                        self.namespace,
                        tail_lines=200,
                        limit_bytes=65_536,
                    )
                )
        except k8s_client.ApiException as exc:
            if exc.status not in (400, 404):
                raise
        match = re.search(
            r"^CURIE_PR_URL=(https://github\.com/[^\s]+/pull/\d+)$",
            logs,
            re.MULTILINE,
        )
        return PublicationJobObservation(
            phase=phase,
            pr_url=match.group(1) if match else None,
            logs=logs,
            error=error,
        )

    def cleanup(self, names: PublicationResourceNames) -> None:
        operations: tuple[tuple[Any, str, dict[str, Any]], ...] = (
            (self._batch.delete_namespaced_job, names.job, {"propagation_policy": "Background"}),
            (self._core.delete_namespaced_secret, names.secret, {}),
            (self._core.delete_namespaced_config_map, names.config_map, {}),
        )
        for delete, name, kwargs in operations:
            try:
                delete(name, self.namespace, **kwargs)
            except k8s_client.ApiException as exc:
                if not self._is_not_found(exc):
                    raise
