"""Secret-free deterministic Kubernetes publication resources."""

from __future__ import annotations

import base64
import importlib
import json
import uuid
from typing import Any

import pytest

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
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["serviceAccountName"] == "curie-publication"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["priorityClassName"] == "curie-platform-critical"
    assert pod_spec["imagePullSecrets"] == [{"name": "registry-creds"}]
    assert container["image"] == "ghcr.io/curie-eng/curie-runner:v0.7.0"
    assert container["command"] == ["/bin/bash", "/publication/publish.sh"]
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
    assert "git clone \"$CLEAN_CLONE_URL\"" in script
    assert "git remote set-url origin \"$CLEAN_CLONE_URL\"" in script
    assert "git -c user.name=\"$GIT_USER_NAME\" -c user.email=\"$GIT_USER_EMAIL\" commit" in script
    assert "git apply --check" in script
    assert "git push" in script
    assert PR_API_URL not in script, "repository-specific URLs must be derived at runtime"
    assert "api.github.com" in script
    assert "gh " not in script
    assert "CURIE_PR_URL=" in script
    assert "redact" in script.lower()
    assert "set -x" not in script
    assert WRITE_CREDENTIAL not in script
    assert WRITE_CREDENTIAL not in serialized_job


def test_job_retries_rest_by_querying_deterministic_head_before_posting_again(
    publication_k8s: Any,
) -> None:
    script = _resources(publication_k8s).config_map["data"]["publish.sh"]
    lookup = script.index("head=")
    post = script.index("POST")

    assert lookup < post
    assert "CURIE_PR_URL=" in script
    assert "urllib" in script or "http.client" in script


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
