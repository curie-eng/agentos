"""Deployment workspace and worker-only clone-credential contracts.

These tests exercise the public deployment API and the private worker redemption
surface against the disposable Postgres database.  The repository selector is
always a stored deployment id: callers never get to aim the operator credential
at an arbitrary origin.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from curie_api.config import get_settings
from curie_api.github_app import _RESOLVERS
from curie_api.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO = "acme-corp/acme-bot"
OTHER_REPO = "attacker/other-bot"
WORKER_TOKEN = "remote-dev-worker-test-token"
WORKER_HEADERS = {"X-Curie-Worker-Token": WORKER_TOKEN}


def _create_agent_version(
    client: TestClient, auth_headers: dict[str, str], *, name: str = "workspace-bot"
) -> tuple[str, str]:
    agent_response = client.post(
        "/agents",
        json={
            "name": name,
            "channel": {"kind": "slack", "address": "C0EXAMPLE1"},
            "repo_full_name": REPO,
        },
        headers=auth_headers,
    )
    assert agent_response.status_code == 201, agent_response.text
    agent_id = agent_response.json()["id"]
    version_response = client.post(
        f"/agents/{agent_id}/versions",
        json={"version_label": "v1", "created_by": "operator"},
        headers=auth_headers,
    )
    assert version_response.status_code == 201, version_response.text
    return agent_id, version_response.json()["id"]


def _deploy(
    client: TestClient,
    auth_headers: dict[str, str],
    agent_id: str,
    version_id: str,
    *,
    workspace: object = ...,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "version_id": version_id,
        "environment": "dev",
    }
    if workspace is not ...:
        body["workspace_repo"] = workspace
    response = client.post("/deployments", json=body, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _audit_rows() -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT purpose, outcome, deployment_id, publication_id, "
                        "repo_full_name, detail FROM "
                        "curie.credential_redemption_audit_entries "
                        "ORDER BY created_at, id"
                    )
                )
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _credential_audit_columns() -> set[str]:
    async def run() -> set[str]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'curie' AND "
                        "table_name = 'credential_redemption_audit_entries'"
                    )
                )
                return set(result.scalars())
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def worker_client(_disposable_db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("APPROVAL_SWEEP_INTERVAL_S", "0")
    monkeypatch.setenv("RESUME_RECONCILER_ENABLED", "false")
    monkeypatch.setenv("DEAD_LETTER_WATCH_INTERVAL_S", "0")
    monkeypatch.setenv("GITHUB_APP_ID", "")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_operator_workspace")
    get_settings.cache_clear()
    _RESOLVERS.clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    _RESOLVERS.clear()
    get_settings.cache_clear()


def test_deployment_workspace_is_sticky_and_explicit_null_disables_it(
    client: TestClient, auth_headers: dict[str, str], clean_db: None
) -> None:
    """Omitted, enabled, and disabled are three distinct deployment writes."""

    agent_id, version_id = _create_agent_version(client, auth_headers)

    enabled = _deploy(client, auth_headers, agent_id, version_id, workspace=REPO)
    carried = _deploy(client, auth_headers, agent_id, version_id)
    disabled = _deploy(client, auth_headers, agent_id, version_id, workspace=None)
    disabled_carried = _deploy(client, auth_headers, agent_id, version_id)

    assert enabled["workspace_repo"] == REPO
    assert carried["workspace_repo"] == REPO
    assert disabled["workspace_repo"] is None
    assert disabled_carried["workspace_repo"] is None

    listed = client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers)
    assert listed.status_code == 200, listed.text
    assert [row["workspace_repo"] for row in listed.json()] == [
        REPO,
        REPO,
        None,
        None,
    ]


@pytest.mark.parametrize(
    "workspace_repo",
    [
        "https://github.com/acme-corp/acme-bot.git",
        "x-access-token:secret@acme-corp/acme-bot",
        "acme-corp/acme-bot/extra",
        " acme-corp/acme-bot ",
        "acme corp/acme-bot",
        "acme-corp/.git",
    ],
)
def test_workspace_repo_accepts_only_one_canonical_owner_repo(
    client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    workspace_repo: str,
) -> None:
    agent_id, version_id = _create_agent_version(client, auth_headers)
    response = client.post(
        "/deployments",
        json={
            "agent_id": agent_id,
            "version_id": version_id,
            "environment": "dev",
            "workspace_repo": workspace_repo,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


def test_workspace_credential_is_worker_only_server_derived_and_no_store(
    worker_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    agent_id, version_id = _create_agent_version(worker_client, auth_headers)
    deployment = _deploy(worker_client, auth_headers, agent_id, version_id, workspace=REPO)
    url = f"/v1/internal/workspaces/{deployment['id']}/credential"

    refused = worker_client.post(url, headers=auth_headers)
    assert refused.status_code == 401
    assert refused.headers["cache-control"] == "no-store"
    assert "ghp_operator_workspace" not in refused.text

    issued = worker_client.post(
        url,
        json={"repo_full_name": OTHER_REPO, "clone_url": "https://evil.example/repo.git"},
        headers=WORKER_HEADERS,
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    assert issued.json() == {
        "repo_full_name": REPO,
        "clone_url": "https://github.com/acme-corp/acme-bot.git",
        "authorization_header": "Basic "
        + base64.b64encode(b"x-access-token:ghp_operator_workspace").decode(),
    }
    assert OTHER_REPO not in issued.text
    assert "evil.example" not in issued.text

    rows = _audit_rows()
    assert [(row["purpose"], row["outcome"]) for row in rows] == [
        ("workspace_clone", "refused"),
        ("workspace_clone", "issued"),
    ]
    assert all(str(row["deployment_id"]) == deployment["id"] for row in rows)
    assert all(row["repo_full_name"] == REPO for row in rows)
    serialized = json.dumps(rows, default=str)
    assert "ghp_operator_workspace" not in serialized
    assert "token" not in _credential_audit_columns()
    assert "authorization" not in _credential_audit_columns()


def test_workspace_credential_pat_fallback_is_redeemed_through_the_endpoint(
    worker_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    agent_id, version_id = _create_agent_version(worker_client, auth_headers)
    deployment = _deploy(worker_client, auth_headers, agent_id, version_id, workspace=REPO)

    response = worker_client.post(
        f"/v1/internal/workspaces/{deployment['id']}/credential",
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["authorization_header"]
        == "Basic " + base64.b64encode(b"x-access-token:ghp_operator_workspace").decode()
    )


def test_workspace_credential_prefers_app_installation_token_over_pat(
    _disposable_db: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_must_not_win")
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("APPROVAL_SWEEP_INTERVAL_S", "0")
    monkeypatch.setenv("RESUME_RECONCILER_ENABLED", "false")
    monkeypatch.setenv("DEAD_LETTER_WATCH_INTERVAL_S", "0")
    get_settings.cache_clear()
    _RESOLVERS.clear()

    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body))
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 4242})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "ghs_repo_scoped",
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(
        "curie_api.github_app.httpx.Client",
        lambda *args, **kwargs: real_client(transport=httpx.MockTransport(handle)),
    )
    with TestClient(create_app()) as app_client:
        agent_id, version_id = _create_agent_version(app_client, auth_headers)
        deployment = _deploy(app_client, auth_headers, agent_id, version_id, workspace=REPO)

        response = app_client.post(
            f"/v1/internal/workspaces/{deployment['id']}/credential",
            headers=WORKER_HEADERS,
        )
        assert response.status_code == 200, response.text
        authorization = response.json()["authorization_header"]
        assert (
            authorization == "Basic " + base64.b64encode(b"x-access-token:ghs_repo_scoped").decode()
        )
        assert base64.b64encode(b"x-access-token:ghp_must_not_win").decode() not in authorization
    assert any(url.endswith(f"/repos/{REPO}/installation") for _, url, _ in calls)
    mint = next(body for _, url, body in calls if url.endswith("/access_tokens"))
    assert mint == {"repositories": ["acme-bot"]}


def test_workspace_credential_requires_a_workspace_enabled_deployment(
    worker_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    agent_id, version_id = _create_agent_version(worker_client, auth_headers)
    deployment = _deploy(worker_client, auth_headers, agent_id, version_id, workspace=None)

    response = worker_client.post(
        f"/v1/internal/workspaces/{deployment['id']}/credential",
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "workspace" in response.json()["detail"].lower()
    audit = _audit_rows()
    assert len(audit) == 1
    assert audit[0]["outcome"] == "refused"
    assert str(audit[0]["deployment_id"]) == deployment["id"]
