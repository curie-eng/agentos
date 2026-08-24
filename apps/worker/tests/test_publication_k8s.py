"""Secret-free deterministic Kubernetes publication resources."""

from __future__ import annotations

import base64
import importlib
import json
import subprocess
import sys
import uuid
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.client import ApiException

PUBLICATION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
WRITE_CREDENTIAL = "publication-write-credential-value"
CLEAN_URL = "https://github.com/acme-corp/acme-bot.git"
PR_API_URL = "https://api.github.com/repos/acme-corp/acme-bot/pulls"


@pytest.fixture
def publication_k8s() -> Any:
    return importlib.import_module("curie_worker.publication_k8s")


def _settings(module: Any) -> Any:
    return module.PublicationJobSettings(
        namespace="curie",
        runner_image="ghcr.io/curie-eng/curie-runner:v0.7.0",
        image_pull_policy="IfNotPresent",
        image_pull_secrets=("registry-creds",),
        priority_class_name="curie-platform-critical",
        service_account_name="curie-publication",
        owner_name="curie-publication-owner",
        git_user_name="Curie Publisher",
        git_user_email="publisher@example.com",
        cpu_request="100m",
        cpu_limit="1",
        memory_request="256Mi",
        memory_limit="1Gi",
        ephemeral_request="1Gi",
        ephemeral_limit="4Gi",
    )


def _payload(module: Any, patch: bytes = b"diff --git a/a b/a\n") -> Any:
    return module.PublicationPayload(
        publication_id=PUBLICATION_ID,
        repo_full_name="acme-corp/acme-bot",
        clean_clone_url=CLEAN_URL,
        base_sha="a" * 40,
        patch=patch,
        branch=module.deterministic_publication_branch(PUBLICATION_ID),
        title="Update repository",
        body="Approved platform publication.",
    )


def _resources(module: Any, patch: bytes = b"diff --git a/a b/a\n") -> Any:
    return module.build_publication_resources(
        _payload(module, patch),
        credential=WRITE_CREDENTIAL,
        settings=_settings(module),
    )


def test_900000_raw_patch_bytes_fit_binary_data_and_900001_is_refused(
    publication_k8s: Any,
) -> None:
    patch = b"x" * 900_000
    resources = _resources(publication_k8s, patch)
    encoded = resources.config_map["binaryData"]["changes.patch"]

    assert base64.b64decode(encoded) == patch
    assert len(base64.b64decode(encoded)) == 900_000
    with pytest.raises(publication_k8s.PublicationResourceError, match="900000"):
        _resources(publication_k8s, b"x" * 900_001)


@pytest.mark.parametrize(
    "base_sha",
    ["abc", "A" * 40, "g" * 40, "a" * 65, "a" * 39, "a" * 40 + ";touch /tmp/x"],
)
def test_publication_base_sha_is_revalidated_before_entering_job_argv(
    publication_k8s: Any,
    base_sha: str,
) -> None:
    payload = _payload(publication_k8s)
    invalid = publication_k8s.PublicationPayload(
        **{**payload.__dict__, "base_sha": base_sha}
    )

    with pytest.raises(publication_k8s.PublicationResourceError, match="base SHA"):
        publication_k8s.build_publication_resources(
            invalid,
            credential=WRITE_CREDENTIAL,
            settings=_settings(publication_k8s),
        )


def test_publication_resource_names_and_branch_are_deterministic(
    publication_k8s: Any,
) -> None:
    first = _resources(publication_k8s)
    second = _resources(publication_k8s)

    assert first.names == second.names
    assert first.job == second.job
    assert first.config_map == second.config_map
    assert first.secret["metadata"]["name"] == first.names.secret
    assert publication_k8s.deterministic_publication_branch(PUBLICATION_ID) == (
        "curie/publication-22222222222242228222222222222222"
    )


def test_built_job_is_bounded_secret_free_and_outside_sandbox_selectors(
    publication_k8s: Any,
) -> None:
    resources = _resources(publication_k8s)
    job = resources.job
    pod = job["spec"]["template"]
    pod_spec = pod["spec"]
    container = pod_spec["containers"][0]
    serialized_public = json.dumps(
        {"job": job, "config_map": resources.config_map}, sort_keys=True
    )

    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "curie-publication"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["priorityClassName"] == "curie-platform-critical"
    assert pod_spec["imagePullSecrets"] == [{"name": "registry-creds"}]
    assert container["image"] == "ghcr.io/curie-eng/curie-runner:v0.7.0"
    assert container["command"] == ["/bin/bash", "/publication/publish.sh"]
    env_by_name = {item["name"]: item["value"] for item in container["env"]}
    assert {
        ("GIT_TIMEOUT_SECONDS", "60"),
        ("GITHUB_TIMEOUT_SECONDS", "30"),
        ("GITHUB_API_URL", "https://api.github.com"),
    } <= set(env_by_name.items())
    assert container["resources"] == {
        "requests": {"cpu": "100m", "memory": "256Mi", "ephemeral-storage": "1Gi"},
        "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "4Gi"},
    }
    assert pod["metadata"]["labels"].get("curietech.ai/component") == "publication"
    assert pod["metadata"]["labels"].get("sandbox.agent-sandbox.io/runner") is None
    assert WRITE_CREDENTIAL not in serialized_public
    assert "secretKeyRef" not in json.dumps(container.get("env", []))
    assert resources.secret["stringData"]["credential"] == WRITE_CREDENTIAL


def test_publish_script_uses_clean_remote_file_askpass_rest_and_redacted_marker(
    publication_k8s: Any,
) -> None:
    resources = _resources(publication_k8s)
    script = resources.config_map["data"]["publish.sh"]
    serialized_job = json.dumps(resources.job)

    assert "GIT_ASKPASS" in script
    assert "/credentials/credential" in script
    assert "git_with_timeout clone \"$CLEAN_CLONE_URL\"" in script
    assert "git_with_timeout remote set-url origin \"$CLEAN_CLONE_URL\"" in script
    assert (
        "git_with_timeout -c user.name=\"$GIT_USER_NAME\" "
        "-c user.email=\"$GIT_USER_EMAIL\" commit" in script
    )
    assert "git_with_timeout apply --check" in script
    assert "git_with_timeout push" in script
    assert (
        'timeout --signal=TERM "${GIT_TIMEOUT_SECONDS}s" '
        'git -c http.followRedirects=false "$@"' in script
    )
    assert 'timeout=int(os.environ["GITHUB_TIMEOUT_SECONDS"])' in script
    assert PR_API_URL not in script, "repository-specific URLs must be derived at runtime"
    assert 'github_api = os.environ["GITHUB_API_URL"].rstrip("/")' in script
    assert 'repo_api = f"{github_api}/repos/{repo}"' in script
    assert "https://api.github.com" not in script
    assert "gh " not in script
    assert "CURIE_PR_URL=" in script
    assert "redact" in script.lower()
    assert "set -x" not in script
    assert WRITE_CREDENTIAL not in script
    assert WRITE_CREDENTIAL not in serialized_job


def test_publish_job_refuses_redirects_for_git_and_github_rest(
    publication_k8s: Any,
) -> None:
    """Neither credential-bearing transport may follow an attacker-controlled redirect."""

    script = _resources(publication_k8s).config_map["data"]["publish.sh"]

    assert 'git -c http.followRedirects=false "$@"' in script
    assert "from urllib.request import HTTPRedirectHandler, Request, build_opener" in script
    assert "class _NoRedirect(HTTPRedirectHandler):" in script
    assert "opener = build_opener(_NoRedirect())" in script
    assert "opener.open(req, timeout=int(os.environ[\"GITHUB_TIMEOUT_SECONDS\"]))" in script
    assert "urlopen(req" not in script


def test_job_injects_a_non_default_github_api_base_without_baking_it_into_script(
    publication_k8s: Any,
) -> None:
    api_base = "https://github.example.com/api/v3"
    resources = publication_k8s.build_publication_resources(
        _payload(publication_k8s),
        credential=WRITE_CREDENTIAL,
        settings=replace(_settings(publication_k8s), github_api_url=api_base),
    )
    container = resources.job["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {item["name"]: item["value"] for item in container["env"]}
    script = resources.config_map["data"]["publish.sh"]

    assert env_by_name["GITHUB_API_URL"] == api_base
    assert api_base not in script
    assert WRITE_CREDENTIAL not in script
    assert WRITE_CREDENTIAL not in json.dumps(resources.job)


def _assert_script_redacts_authorization(script: str) -> None:
    redact_function = script[
        script.index("redact() {") : script.index("\n}\n\ngit_with_timeout") + 2
    ]
    sensitive = (
        "Authorization: Basic dXNlcjp3cml0ZS10b2tlbg==\n"
        "authorization: Bearer github_pat_sensitive\n"
    )
    completed = subprocess.run(
        ["/bin/bash", "-c", f"{redact_function}\nredact"],
        input=sensitive,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout == "Authorization: [REDACTED]\nAuthorization: [REDACTED]\n"
    assert "dXNlc" not in completed.stdout
    assert "github_pat_sensitive" not in completed.stdout


def test_publish_script_executes_redaction_and_the_assertion_catches_a_mutation(
    publication_k8s: Any,
) -> None:
    script = _resources(publication_k8s).config_map["data"]["publish.sh"]

    subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
    _assert_script_redacts_authorization(script)
    mutation = script.replace("[Bb][Ee][Aa][Rr][Ee][Rr]", "[Xx][Ee][Aa][Rr][Ee][Rr]")
    with pytest.raises(AssertionError):
        _assert_script_redacts_authorization(mutation)


def test_job_retries_rest_by_querying_deterministic_head_before_posting_again(
    publication_k8s: Any,
) -> None:
    script = _resources(publication_k8s).config_map["data"]["publish.sh"]
    lookup = script.index("head=")
    post = script.index("POST")

    assert lookup < post
    assert "CURIE_PR_URL=" in script
    assert "urllib" in script or "http.client" in script


def _run_job_pull_validator(
    script: str, row: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    start = script.index("def validate_pull(")
    end = script.index("\ndef existing(", start)
    validator = script[start:end]
    program = f"""
import json
import os
import sys
repo = {json.dumps('acme-corp/acme-bot')}
branch = {json.dumps('curie/publication-22222222222242228222222222222222')}
{validator}
print(validate_pull(json.loads(sys.stdin.read()), "main"))
"""
    return subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(row),
        text=True,
        capture_output=True,
        env={
            "PR_TITLE": "Update repository",
            "PR_BODY": "Approved platform publication.",
        },
        check=False,
    )


def _job_pull_row() -> dict[str, object]:
    return {
        "html_url": "https://github.com/acme-corp/acme-bot/pull/123",
        "title": "Update repository",
        "body": "Approved platform publication.",
        "head": {
            "ref": "curie/publication-22222222222242228222222222222222",
            "repo": {"full_name": "acme-corp/acme-bot"},
        },
        "base": {"ref": "main", "repo": {"full_name": "acme-corp/acme-bot"}},
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (("title", "Mutated title"), ("body", "Mutated body")),
)
def test_job_rejects_mutated_pull_request_metadata(
    publication_k8s: Any,
    field: str,
    value: str,
) -> None:
    script = _resources(publication_k8s).config_map["data"]["publish.sh"]
    valid = _run_job_pull_validator(script, _job_pull_row())
    assert valid.returncode == 0, valid.stderr

    mutated = _job_pull_row()
    mutated[field] = value
    rejected = _run_job_pull_validator(script, mutated)

    assert rejected.returncode != 0
    assert "approved publication contract" in rejected.stderr


def test_every_dynamic_resource_has_the_helm_owner_reference(
    publication_k8s: Any,
) -> None:
    resources = _resources(publication_k8s)
    for obj in (resources.config_map, resources.secret, resources.job):
        refs = obj["metadata"]["ownerReferences"]
        assert refs == [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "name": "curie-publication-owner",
                "uid": resources.owner_uid,
                "controller": False,
                "blockOwnerDeletion": False,
            }
        ]


class _FakeCoreApi:
    def __init__(self, owner_name: str) -> None:
        self.config_maps: dict[str, dict[str, Any]] = {
            owner_name: {"metadata": {"name": owner_name, "uid": "live-owner-uid"}}
        }
        self.secrets: dict[str, dict[str, Any]] = {}
        self.created: list[tuple[str, str]] = []
        self.secret_deletes: list[tuple[str, dict[str, Any]]] = []

    def _read(self, rows: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
        if name not in rows:
            raise ApiException(status=404)
        return deepcopy(rows[name])

    def read_namespaced_config_map(self, name: str, namespace: str) -> dict[str, Any]:
        return self._read(self.config_maps, name)

    def read_namespaced_secret(self, name: str, namespace: str) -> dict[str, Any]:
        return self._read(self.secrets, name)

    def create_namespaced_config_map(self, namespace: str, body: dict[str, Any]) -> None:
        value = deepcopy(body)
        value["metadata"]["uid"] = f"uid-{body['metadata']['name']}"
        self.config_maps[body["metadata"]["name"]] = value
        self.created.append(("ConfigMap", body["metadata"]["name"]))

    def create_namespaced_secret(self, namespace: str, body: dict[str, Any]) -> None:
        value = deepcopy(body)
        value["metadata"]["uid"] = f"uid-{body['metadata']['name']}"
        value["data"] = {
            key: base64.b64encode(raw.encode()).decode()
            for key, raw in value.pop("stringData").items()
        }
        self.secrets[body["metadata"]["name"]] = value
        self.created.append(("Secret", body["metadata"]["name"]))

    def delete_namespaced_secret(
        self, name: str, namespace: str, *, body: dict[str, Any]
    ) -> None:
        self.secret_deletes.append((name, deepcopy(body)))
        del self.secrets[name]


class _FakeBatchApi:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.created: list[str] = []

    def read_namespaced_job(self, name: str, namespace: str) -> dict[str, Any]:
        if name not in self.jobs:
            raise ApiException(status=404)
        return deepcopy(self.jobs[name])

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> None:
        value = deepcopy(body)
        value["metadata"]["uid"] = f"uid-{body['metadata']['name']}"
        self.jobs[body["metadata"]["name"]] = value
        self.created.append(body["metadata"]["name"])


def _fake_cluster(module: Any) -> tuple[Any, _FakeCoreApi, _FakeBatchApi]:
    cluster = object.__new__(module.KubernetesPublicationCluster)
    cluster.namespace = "curie"
    core = _FakeCoreApi("curie-publication-owner")
    batch = _FakeBatchApi()
    cluster._core = core
    cluster._batch = batch
    return cluster, core, batch


def test_create_or_adopt_validates_immutable_spec_and_live_owner_before_mutating(
    publication_k8s: Any,
) -> None:
    cluster, core, batch = _fake_cluster(publication_k8s)
    resources = _resources(publication_k8s)
    cluster.apply(resources)
    first_creates = (list(core.created), list(batch.created))

    cluster.apply(resources)
    assert (core.created, batch.created) == first_creates

    batch.jobs[resources.names.job]["spec"]["backoffLimit"] = 1
    with pytest.raises(publication_k8s.PublicationResourceError, match="spec mismatch"):
        cluster.apply(resources)
    assert (core.created, batch.created) == first_creates

    batch.jobs[resources.names.job]["spec"]["backoffLimit"] = 0
    original_credential = core.secrets[resources.names.secret]["data"]["credential"]
    core.secrets[resources.names.secret]["data"]["credential"] = base64.b64encode(
        b"collision-credential"
    ).decode()
    with pytest.raises(publication_k8s.PublicationResourceError, match="credential mismatch"):
        cluster.apply(resources)
    core.secrets[resources.names.secret]["data"]["credential"] = original_credential

    core.config_maps[resources.names.config_map]["metadata"]["ownerReferences"][0][
        "uid"
    ] = "different-owner-uid"
    with pytest.raises(publication_k8s.PublicationResourceError, match="metadata contract"):
        cluster.apply(resources)


def test_stale_immutable_secret_is_uid_replaced_only_when_job_is_gone(
    publication_k8s: Any,
) -> None:
    cluster, core, batch = _fake_cluster(publication_k8s)
    resources = _resources(publication_k8s)
    cluster.apply(resources)
    old_uid = core.secrets[resources.names.secret]["metadata"]["uid"]
    del batch.jobs[resources.names.job]
    rotated = publication_k8s.build_publication_resources(
        _payload(publication_k8s),
        credential="rotated-publication-write-credential",
        settings=_settings(publication_k8s),
    )

    cluster.apply(rotated)

    assert core.secret_deletes == [
        (resources.names.secret, {"preconditions": {"uid": old_uid}})
    ]
    assert batch.created == [resources.names.job, resources.names.job]
    assert base64.b64decode(
        core.secrets[resources.names.secret]["data"]["credential"]
    ).decode() == "rotated-publication-write-credential"


def test_observe_reads_logs_only_from_a_pod_owned_by_the_exact_job(
    publication_k8s: Any,
) -> None:
    cluster = object.__new__(publication_k8s.KubernetesPublicationCluster)
    cluster.namespace = "curie-publications"
    job_uid = "job-uid-123"
    cluster._batch = SimpleNamespace(
        read_namespaced_job=lambda *_args: SimpleNamespace(
            metadata=SimpleNamespace(uid=job_uid),
            status=SimpleNamespace(succeeded=1, failed=0, conditions=[]),
        )
    )
    log_reads: list[str] = []
    hostile = SimpleNamespace(
        metadata=SimpleNamespace(
            name="hostile-pod",
            owner_references=[SimpleNamespace(kind="Job", uid="different-job")],
        )
    )
    owned = SimpleNamespace(
        metadata=SimpleNamespace(
            name="owned-pod",
            owner_references=[SimpleNamespace(kind="Job", uid=job_uid)],
        )
    )

    def read_log(name: str, *_args: object, **_kwargs: object) -> str:
        log_reads.append(name)
        if name == "hostile-pod":
            return "CURIE_PR_URL=https://github.com/attacker/repo/pull/1"
        return "CURIE_PR_URL=https://github.com/acme-corp/acme-bot/pull/123"

    cluster._core = SimpleNamespace(
        list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(
            items=[hostile, owned]
        ),
        read_namespaced_pod_log=read_log,
    )

    observed = cluster.observe("curie-publication-22222222222242228222")

    assert log_reads == ["owned-pod"]
    assert observed.pr_url == "https://github.com/acme-corp/acme-bot/pull/123"


def test_observe_reads_terminal_status_from_dict_shaped_kubernetes_objects(
    publication_k8s: Any,
) -> None:
    cluster = object.__new__(publication_k8s.KubernetesPublicationCluster)
    cluster.namespace = "curie-publications"
    cluster._batch = SimpleNamespace(
        read_namespaced_job=lambda *_args: {
            "metadata": {"uid": "job-uid-123"},
            "status": {
                "succeeded": 0,
                "failed": 1,
                "conditions": [
                    {"status": "True", "reason": "DeadlineExceeded"}
                ],
            },
        }
    )
    cluster._core = SimpleNamespace(
        list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(items=[])
    )

    observed = cluster.observe("curie-publication-22222222222242228222")

    assert observed.phase == "failed"
    assert observed.error == "DeadlineExceeded"


def test_resource_builder_binds_clone_url_to_publication_repository(
    publication_k8s: Any,
) -> None:
    payload = _payload(publication_k8s)
    payload = publication_k8s.PublicationPayload(
        **{
            **payload.__dict__,
            "clean_clone_url": "https://github.com/other-corp/other-repo.git",
        }
    )

    with pytest.raises(publication_k8s.PublicationResourceError, match="clone URL"):
        publication_k8s.build_publication_resources(
            payload,
            credential="publication-write-credential",
            settings=_settings(publication_k8s),
        )


def test_publication_cluster_has_no_legacy_combined_cleanup_shim(
    publication_k8s: Any,
) -> None:
    assert not hasattr(publication_k8s.KubernetesPublicationCluster, "cleanup")
