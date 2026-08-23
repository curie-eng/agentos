"""Deterministic, secret-minimizing Kubernetes publication resources.

The patch is carried as ConfigMap ``binaryData`` and the short-lived GitHub
credential lives only in a Secret volume.  Neither value appears in Job argv,
environment, labels, or logs.  Names are publication-id-derived so a worker
restart adopts the same resource set instead of starting a second push.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

if TYPE_CHECKING:
    from .publication_loop import PublicationJobObservation

MAX_PATCH_BYTES = 900_000
_AUTHORIZATION_LOG = re.compile(
    r"Authorization:\s*(?:Basic|Bearer|token)\s+[^\s]+", re.IGNORECASE
)


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
    active_deadline_seconds: int = 300
    git_timeout_seconds: int = 60
    github_timeout_seconds: int = 30
    github_api_url: str = "https://api.github.com"


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


def publication_resource_names(publication_id: uuid.UUID) -> PublicationResourceNames:
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
  auth_scheme='([Bb][Aa][Ss][Ii][Cc]|[Bb][Ee][Aa][Rr][Ee][Rr]|'
  auth_scheme+='[Tt][Oo][Kk][Ee][Nn])'
  sed -E -e 's#https://[^/@[:space:]]*@github\.com#https://github.com#g' \
      -e "s#Authorization:[[:space:]]*${auth_scheme}"\
"[[:space:]]+[^[:space:]]+#Authorization: [REDACTED]#gI"
}

git_with_timeout() {
  timeout --signal=TERM "${GIT_TIMEOUT_SECONDS}s" git "$@"
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
git_with_timeout clone "$CLEAN_CLONE_URL" repo 2> >(redact >&2)
cd repo
git_with_timeout remote set-url origin "$CLEAN_CLONE_URL"
git_with_timeout config --get remote.origin.url | grep -Fx "$CLEAN_CLONE_URL" >/dev/null
if ! git_with_timeout cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
  git_with_timeout fetch --depth=1 origin "$BASE_SHA" 2> >(redact >&2)
fi
git_with_timeout checkout --detach "$BASE_SHA" 2> >(redact >&2)
git_with_timeout switch -c "$BRANCH" 2> >(redact >&2)
git_with_timeout apply --check --binary /publication/changes.patch
git_with_timeout apply --binary /publication/changes.patch
git_with_timeout add --all
if git_with_timeout diff --cached --quiet; then
  echo "publication patch produced no changes" >&2
  exit 1
fi
git_with_timeout -c user.name="$GIT_USER_NAME" -c user.email="$GIT_USER_EMAIL" commit -m "$PR_TITLE"
git_with_timeout push origin "HEAD:refs/heads/$BRANCH" 2> >(redact >&2)

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
github_api = os.environ["GITHUB_API_URL"].rstrip("/")
repo_api = f"{github_api}/repos/{repo}"
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
    with urlopen(req, timeout=int(os.environ["GITHUB_TIMEOUT_SECONDS"])) as response:
        return json.load(response)

def validate_pull(row, expected_base):
    if not isinstance(row, dict):
        raise SystemExit("GitHub returned an invalid pull request")
    head = row.get("head")
    base = row.get("base")
    expected = {
        "title": os.environ["PR_TITLE"],
        "body": os.environ["PR_BODY"],
        "head_ref": branch,
        "head_repo": repo,
        "base_ref": expected_base,
        "base_repo": repo,
    }
    actual = {
        "title": row.get("title"),
        "body": row.get("body"),
        "head_ref": head.get("ref") if isinstance(head, dict) else None,
        "head_repo": (
            (head.get("repo") or {}).get("full_name")
            if isinstance(head, dict) and isinstance(head.get("repo"), dict)
            else None
        ),
        "base_ref": base.get("ref") if isinstance(base, dict) else None,
        "base_repo": (
            (base.get("repo") or {}).get("full_name")
            if isinstance(base, dict) and isinstance(base.get("repo"), dict)
            else None
        ),
    }
    if actual != expected:
        raise SystemExit(
            "GitHub pull request does not match the approved publication contract"
        )
    url = row.get("html_url")
    prefix = f"https://github.com/{repo}/pull/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise SystemExit("GitHub did not return a usable pull request URL")
    number = url.removeprefix(prefix)
    if not number.isdigit() or int(number) <= 0:
        raise SystemExit("GitHub did not return a usable pull request URL")
    return url

def existing(expected_base):
    head=quote(f"{owner}:{branch}", safe="")
    rows = request("GET", f"{api}?state=open&head={head}")
    return validate_pull(rows[0], expected_base) if rows else None

# Query the deterministic head before POST, and again after an ambiguous REST
# failure. This is the idempotency boundary for a lost response.
repository = request("GET", repo_api)
default_base = repository.get("default_branch")
if not isinstance(default_base, str) or not default_base:
    raise SystemExit("GitHub repository has no default branch")
url = existing(default_base)
if not url:
    try:
        created = request("POST", api, {
            "title": os.environ["PR_TITLE"],
            "head": branch,
            "base": default_base,
            "body": os.environ["PR_BODY"],
        })
        url = validate_pull(created, default_base)
    except (HTTPError, URLError):
        url = existing(default_base)
        if not url:
            raise
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
    if re.fullmatch(r"[0-9a-f]{40,64}", payload.base_sha) is None:
        raise PublicationResourceError(
            "publication base SHA must be 40-64 lowercase hexadecimal characters"
        )
    if not credential.strip():
        raise PublicationResourceError("publication credential is empty")
    if min(
        settings.active_deadline_seconds,
        settings.git_timeout_seconds,
        settings.github_timeout_seconds,
    ) <= 0:
        raise PublicationResourceError("publication timeouts must be positive")
    if not settings.github_api_url.startswith("https://"):
        raise PublicationResourceError("publication GitHub API URL must use HTTPS")
    names = publication_resource_names(payload.publication_id)
    owner_uid = settings.owner_uid or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"curie:{settings.namespace}:{settings.owner_name}")
    )
    labels = {
        "app.kubernetes.io/managed-by": "curie",
        "curietech.ai/component": "publication",
        "curietech.ai/publication-id": str(payload.publication_id),
    }
    contract = {
        "publication_id": str(payload.publication_id),
        "repo_full_name": payload.repo_full_name,
        "clean_clone_url": payload.clean_clone_url,
        "base_sha": payload.base_sha,
        "patch_sha256": hashlib.sha256(payload.patch).hexdigest(),
        "branch": payload.branch,
        "title": payload.title,
        "body": payload.body,
        "runner_image": settings.runner_image,
        "service_account_name": settings.service_account_name,
        "active_deadline_seconds": settings.active_deadline_seconds,
        "git_timeout_seconds": settings.git_timeout_seconds,
        "github_timeout_seconds": settings.github_timeout_seconds,
        "github_api_url": settings.github_api_url,
    }
    annotations = {
        "curietech.ai/publication-contract-sha256": hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    }
    metadata: dict[str, Any] = {
        "namespace": settings.namespace,
        "labels": labels,
        "annotations": annotations,
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
        {"name": "GIT_TIMEOUT_SECONDS", "value": str(settings.git_timeout_seconds)},
        {"name": "GITHUB_TIMEOUT_SECONDS", "value": str(settings.github_timeout_seconds)},
        {"name": "GITHUB_API_URL", "value": settings.github_api_url},
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
            "activeDeadlineSeconds": settings.active_deadline_seconds,
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


def _as_serialized_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return copy.deepcopy(obj)
    serialized = k8s_client.ApiClient().sanitize_for_serialization(obj)
    if not isinstance(serialized, dict):
        raise PublicationResourceError("Kubernetes returned a non-object publication resource")
    return serialized


def _expected_projection(observed: Any, expected: Any) -> Any:
    """Project API-defaulted state onto the immutable submitted contract."""

    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return observed
        return {
            key: _expected_projection(observed.get(key), value)
            for key, value in expected.items()
        }
    if isinstance(expected, list):
        if not isinstance(observed, list) or len(observed) != len(expected):
            return observed
        return [
            _expected_projection(observed_value, expected_value)
            for observed_value, expected_value in zip(observed, expected, strict=True)
        ]
    return observed


def validate_adopted_resource(
    kind: str,
    expected: dict[str, Any],
    observed: Any,
    *,
    compare_secret_value: bool = True,
) -> None:
    """Fail closed unless an exact-name object carries our immutable contract.

    Kubernetes defaults fields after creation, so Jobs compare the submitted
    spec projection plus explicit pod escape hatches. Secret credential bytes
    are compared without logging while a matching Job exists. If its Job no
    longer exists, ``apply`` uses only an exact-name metadata/key-shape GET
    (never a list), then UID-replaces the immutable Secret with the freshly
    redeemed credential before creating another Job.
    """

    actual = _as_serialized_mapping(observed)
    expected_metadata = expected["metadata"]
    metadata_contract = {
        key: expected_metadata[key]
        for key in ("name", "namespace", "labels", "annotations", "ownerReferences")
    }
    if _expected_projection(actual.get("metadata"), metadata_contract) != metadata_contract:
        raise PublicationResourceError(
            f"refusing to adopt {kind} {expected_metadata['name']!r}: metadata contract mismatch"
        )

    if kind == "ConfigMap":
        contract = {
            "immutable": expected["immutable"],
            "binaryData": expected["binaryData"],
            "data": expected["data"],
        }
        if _expected_projection(actual, contract) != contract:
            raise PublicationResourceError(
                f"refusing to adopt ConfigMap {expected_metadata['name']!r}: payload mismatch"
            )
        return

    if kind == "Secret":
        actual_keys = set((actual.get("data") or actual.get("stringData") or {}).keys())
        if (
            actual.get("immutable") is not True
            or actual.get("type") != expected["type"]
            or actual_keys != {"credential"}
        ):
            raise PublicationResourceError(
                f"refusing to adopt Secret {expected_metadata['name']!r}: immutable shape mismatch"
            )
        if compare_secret_value:
            encoded = (actual.get("data") or {}).get("credential")
            if not isinstance(encoded, str) or not encoded:
                raise PublicationResourceError(
                    f"refusing to adopt Secret {expected_metadata['name']!r}: credential mismatch"
                )
            try:
                actual_credential = base64.b64decode(encoded, validate=True).decode()
            except (TypeError, ValueError, UnicodeError) as exc:
                raise PublicationResourceError(
                    f"refusing to adopt Secret {expected_metadata['name']!r}: credential mismatch"
                ) from exc
            expected_credential = str(expected["stringData"]["credential"])
            if not hmac.compare_digest(actual_credential, expected_credential):
                raise PublicationResourceError(
                    f"refusing to adopt Secret {expected_metadata['name']!r}: credential mismatch"
                )
        return

    if kind != "Job":
        raise PublicationResourceError(f"unknown publication resource kind {kind!r}")
    if _expected_projection(actual.get("spec"), expected["spec"]) != expected["spec"]:
        raise PublicationResourceError(
            f"refusing to adopt Job {expected_metadata['name']!r}: spec mismatch"
        )
    pod_spec = ((actual.get("spec") or {}).get("template") or {}).get("spec") or {}
    forbidden = {
        "hostNetwork": pod_spec.get("hostNetwork"),
        "hostPID": pod_spec.get("hostPID"),
        "hostIPC": pod_spec.get("hostIPC"),
        "shareProcessNamespace": pod_spec.get("shareProcessNamespace"),
        "initContainers": pod_spec.get("initContainers"),
        "ephemeralContainers": pod_spec.get("ephemeralContainers"),
    }
    if any(value not in (None, False, []) for value in forbidden.values()):
        raise PublicationResourceError(
            f"refusing to adopt Job {expected_metadata['name']!r}: pod security mismatch"
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
        if isinstance(owner, dict):
            uid = (owner.get("metadata") or {}).get("uid")
        else:
            uid = getattr(getattr(owner, "metadata", None), "uid", None)
        if not uid:
            raise PublicationResourceError(
                f"publication owner ConfigMap {owner_name!r} has no UID"
            )
        return str(uid)

    @staticmethod
    def _is_not_found(exc: k8s_client.ApiException) -> bool:
        return bool(exc.status == 404)

    def _read_existing(self, kind: str, obj: dict[str, Any]) -> Any | None:
        name = str(obj["metadata"]["name"])
        api = self._batch if kind == "Job" else self._core
        suffix = {"ConfigMap": "config_map", "Secret": "secret", "Job": "job"}[kind]
        try:
            return getattr(api, f"read_namespaced_{suffix}")(name, self.namespace)
        except k8s_client.ApiException as exc:
            if not self._is_not_found(exc):
                raise
            return None

    def _create(self, kind: str, obj: dict[str, Any]) -> None:
        api = self._batch if kind == "Job" else self._core
        suffix = {"ConfigMap": "config_map", "Secret": "secret", "Job": "job"}[kind]
        getattr(api, f"create_namespaced_{suffix}")(self.namespace, body=obj)

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
        existing = {
            str(obj["kind"]): self._read_existing(str(obj["kind"]), obj) for obj in objects
        }
        # Validate every collision before making any mutation. This prevents a
        # matching label on a hostile or stale object from authorizing partial
        # adoption. Reads are exact-name GETs; publication RBAC needs no Secret
        # list permission.
        for obj in objects:
            kind = str(obj["kind"])
            if existing[kind] is not None:
                validate_adopted_resource(
                    kind,
                    obj,
                    existing[kind],
                    # A stale immutable Secret is replaced by UID below when
                    # no Job exists, so reading/comparing credential bytes is
                    # unnecessary in that recovery shape.
                    compare_secret_value=not (
                        kind == "Secret" and existing["Job"] is None
                    ),
                )

        config_map, secret, job = objects
        if existing["ConfigMap"] is None:
            self._create("ConfigMap", config_map)

        if existing["Secret"] is not None and existing["Job"] is None:
            # A finished Job may be removed by its TTL while its immutable
            # credential Secret survives a worker crash. Replace by UID before
            # starting another Job; never silently reuse unknown credential
            # bytes with a newly redeemed installation token.
            serialized_secret = _as_serialized_mapping(existing["Secret"])
            secret_uid = (serialized_secret.get("metadata") or {}).get("uid")
            if not secret_uid:
                raise PublicationResourceError(
                    f"refusing to replace Secret {resources.names.secret!r} without a stable UID"
                )
            self._core.delete_namespaced_secret(
                resources.names.secret,
                self.namespace,
                body={"preconditions": {"uid": secret_uid}},
            )
            existing["Secret"] = None
        if existing["Secret"] is None:
            self._create("Secret", secret)
        if existing["Job"] is None:
            self._create("Job", job)

    def observe(self, job_name: str) -> PublicationJobObservation:
        # Local import avoids a module import cycle: publication_loop owns the
        # neutral observation DTO while this module owns Kubernetes shapes.
        from .publication_loop import PublicationJobObservation

        try:
            job = self._batch.read_namespaced_job(job_name, self.namespace)
        except k8s_client.ApiException as exc:
            if self._is_not_found(exc):
                return PublicationJobObservation(
                    phase="pending", pr_url=None, logs="", error=None, exists=False
                )
            raise
        metadata = job.get("metadata") if isinstance(job, dict) else getattr(job, "metadata", None)
        job_uid = (
            metadata.get("uid")
            if isinstance(metadata, dict)
            else getattr(metadata, "uid", None)
        )
        if not job_uid:
            raise PublicationResourceError(
                f"publication Job {job_name!r} has no stable UID"
            )
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
            owned = []
            for pod in items:
                pod_metadata = (
                    pod.get("metadata")
                    if isinstance(pod, dict)
                    else getattr(pod, "metadata", None)
                )
                references = (
                    pod_metadata.get("ownerReferences", [])
                    if isinstance(pod_metadata, dict)
                    else getattr(pod_metadata, "owner_references", None) or []
                )
                if any(
                    (
                        reference.get("kind") == "Job"
                        and str(reference.get("uid")) == str(job_uid)
                    )
                    if isinstance(reference, dict)
                    else (
                        getattr(reference, "kind", None) == "Job"
                        and str(getattr(reference, "uid", None)) == str(job_uid)
                    )
                    for reference in references
                ):
                    owned.append(pod)
            if owned:
                owned.sort(
                    key=lambda pod: str(
                        (pod.get("metadata") or {}).get("name")
                        if isinstance(pod, dict)
                        else getattr(getattr(pod, "metadata", None), "name", "")
                    )
                )
                selected = owned[0]
                selected_metadata = (
                    selected.get("metadata")
                    if isinstance(selected, dict)
                    else getattr(selected, "metadata", None)
                )
                pod_name = str(
                    selected_metadata.get("name")
                    if isinstance(selected_metadata, dict)
                    else getattr(selected_metadata, "name", "")
                )
                if not pod_name:
                    raise PublicationResourceError(
                        f"publication Job {job_name!r} owns a pod without a name"
                    )
                logs = str(
                    self._core.read_namespaced_pod_log(
                        pod_name,
                        self.namespace,
                        tail_lines=200,
                        limit_bytes=65_536,
                    )
                )
                logs = _AUTHORIZATION_LOG.sub("Authorization: [REDACTED]", logs)
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

    def cleanup_credentials(self, names: PublicationResourceNames) -> None:
        try:
            self._core.delete_namespaced_secret(names.secret, self.namespace)
        except k8s_client.ApiException as exc:
            if not self._is_not_found(exc):
                raise

    def cleanup_terminal(self, names: PublicationResourceNames) -> None:
        operations: tuple[tuple[Any, str, dict[str, Any]], ...] = (
            (self._batch.delete_namespaced_job, names.job, {"propagation_policy": "Background"}),
            (self._core.delete_namespaced_config_map, names.config_map, {}),
        )
        for delete, name, kwargs in operations:
            try:
                delete(name, self.namespace, **kwargs)
            except k8s_client.ApiException as exc:
                if not self._is_not_found(exc):
                    raise
