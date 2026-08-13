"""KubernetesSandboxClient.create_claim payload shape.

The agent-sandbox controller injects per-claim env with no ``containerName`` into
only the first main container. The bundle ref must ALSO be targeted at the init
containers by name, or a Kubernetes runner boots an empty plugin dir. These tests
assert the emitted SandboxClaim env, so the fix is mutation-honest: dropping the
named entries fails ``test_bundle_ref_targets_init_containers_by_name``.
"""

from __future__ import annotations

from typing import Any

from curie_worker.sandbox.k8s import (
    BUNDLE_INIT_CONTAINERS,
    KubernetesSandboxClient,
)


class _FakeApi:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> None:
        self.created.append(body)


def _client(api: _FakeApi) -> KubernetesSandboxClient:
    client = KubernetesSandboxClient.__new__(KubernetesSandboxClient)
    client._api = api  # type: ignore[attr-defined]
    client._namespace = "test-ns"  # type: ignore[attr-defined]
    return client


def _env_entries(api: _FakeApi) -> list[dict[str, str]]:
    return api.created[0]["spec"]["env"]


def test_bundle_ref_targets_init_containers_by_name() -> None:
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={"CURIE_BUNDLE_REF": "bundles/x.tar.gz", "CURIE_BUDGET": "{}"},
    )
    entries = _env_entries(api)

    # The main runner still receives the ref (unnamed entry).
    assert {"name": "CURIE_BUNDLE_REF", "value": "bundles/x.tar.gz"} in entries

    # And each bundle init container receives it by explicit containerName.
    named = {
        (e["containerName"], e["name"]): e["value"] for e in entries if "containerName" in e
    }
    for container in BUNDLE_INIT_CONTAINERS:
        assert named[(container, "CURIE_BUNDLE_REF")] == "bundles/x.tar.gz"


def test_no_named_env_without_bundle_ref() -> None:
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1", pool="pool", env={"CURIE_BUDGET": "{}", "CURIE_SESSION_ID": "s"}
    )
    entries = _env_entries(api)
    assert entries  # the main-container env is still present
    assert all("containerName" not in e for e in entries)


def test_credential_is_never_written_to_the_claim() -> None:
    # The SandboxClaim env is value-only, so the secret must not be persisted on
    # the claim; the template's secretKeyRef supplies it to the runner instead.
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={"CURIE_BUDGET": "{}", "CURIE_CREDENTIALS": "super-secret-token"},
    )
    entries = _env_entries(api)
    assert all(e.get("name") != "CURIE_CREDENTIALS" for e in entries)
    assert all("super-secret-token" not in e.get("value", "") for e in entries)
    # The rest of the boot env is still written.
    assert {"name": "CURIE_BUDGET", "value": "{}"} in entries


def test_runner_token_is_a_plaintext_env_entry_credential_excluded() -> None:
    # The per-sandbox runner token intentionally rides the generic env loop as a
    # plaintext {name, value} entry on the claim (there is no secretKeyRef path
    # for it), while the model credential stays excluded.
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={
            "CURIE_BUDGET": "{}",
            "CURIE_RUNNER_TOKEN": "tok-26",
            "CURIE_CREDENTIALS": "super-secret-token",
        },
    )
    entries = _env_entries(api)
    assert {"name": "CURIE_RUNNER_TOKEN", "value": "tok-26"} in entries
    assert all(e.get("name") != "CURIE_CREDENTIALS" for e in entries)


def test_connector_secrets_are_never_written_to_the_claim() -> None:
    # Per-agent connector secrets (#429) ride the substrate-agnostic boot env by
    # value, but the value-only claim CR would persist them in plaintext in etcd.
    # The binding marks their keys in CURIE_CONNECTOR_SECRET_KEYS; the substrate
    # strips both the marker and every key it names (cluster delivery is #440).
    api = _FakeApi()
    _client(api).create_claim(
        "claim-1",
        pool="pool",
        env={
            "CURIE_BUDGET": "{}",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_super_secret",
            "API_KEY": "k-secret",
            "CURIE_CONNECTOR_SECRET_KEYS": "API_KEY,GITHUB_PERSONAL_ACCESS_TOKEN",
        },
    )
    entries = _env_entries(api)
    # Neither the secret values nor the marker land on the claim.
    for leaked in ("ghp_super_secret", "k-secret"):
        assert all(leaked not in e.get("value", "") for e in entries)
    names = {e.get("name") for e in entries}
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in names
    assert "API_KEY" not in names
    assert "CURIE_CONNECTOR_SECRET_KEYS" not in names
    # Non-secret boot env is still written.
    assert {"name": "CURIE_BUDGET", "value": "{}"} in entries
