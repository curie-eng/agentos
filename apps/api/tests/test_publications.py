"""Durable, approval-gated publication control-plane contracts.

The API owns only durable state and operator-credential redemption.  Resolving a
publication approval never enqueues a model turn and never performs Kubernetes
or GitHub side effects; the worker reconciles the durable publication row.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
from curie_api import crud
from curie_api.config import get_settings
from curie_api.github_app import _RESOLVERS
from curie_api.main import create_app
from curie_api.resumequeue import ResumeQueue
from curie_api.sweeper import sweep_expired_approvals
from curie_test_support.valkey import connect_or_skip
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = "acme-corp/acme-bot"
WORKER_TOKEN = "remote-dev-publication-worker-token"
WORKER_HEADERS = {"X-Curie-Worker-Token": WORKER_TOKEN}
PATCH_LIMIT = 900_000
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def publication_stack(
    _disposable_db: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
    runs_stream = f"test:curie:publication-runs:{uuid.uuid4().hex}"
    monkeypatch.setenv("RUNS_STREAM", runs_stream)
    monkeypatch.setenv("INTERNAL_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("APPROVAL_SWEEP_INTERVAL_S", "0")
    monkeypatch.setenv("RESUME_RECONCILER_ENABLED", "false")
    monkeypatch.setenv("DEAD_LETTER_WATCH_INTERVAL_S", "0")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_publication_operator")
    get_settings.cache_clear()
    _RESOLVERS.clear()
    with TestClient(create_app()) as test_client:
        yield test_client, runs_stream
    valkey = connect_or_skip(decode_responses=True)
    valkey.delete(runs_stream, f"{runs_stream}:dead")
    valkey.close()
    _RESOLVERS.clear()
    get_settings.cache_clear()


def _create_deployment(
    client: TestClient, auth_headers: dict[str, str], *, name: str | None = None
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    agent_response = client.post(
        "/agents",
        json={
            "name": name or f"publisher-{suffix}",
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
    deployment_response = client.post(
        "/deployments",
        json={
            "agent_id": agent_id,
            "version_id": version_response.json()["id"],
            "environment": "dev",
            "workspace_repo": REPO,
        },
        headers=auth_headers,
    )
    assert deployment_response.status_code == 201, deployment_response.text
    return deployment_response.json()


def _publication_payload(
    deployment_id: str,
    *,
    patch: bytes = b"diff --git a/README.md b/README.md\n",
    dedupe_key: str | None = None,
    author: str = "U0REQUEST1",
    expires_in_seconds: int | None = 600,
) -> dict[str, Any]:
    return {
        "deployment_id": deployment_id,
        "conversation_id": f"thread-{uuid.uuid4().hex[:8]}",
        "author": author,
        "summary": "Publish the repository changes",
        "reply_kind": "slack",
        "reply_channel": "C0EXAMPLE1",
        "reply_placeholder": "1700000000.000001",
        "dedupe_key": dedupe_key or f"publish-{uuid.uuid4().hex}",
        "base_sha": BASE_SHA,
        "patch_b64": base64.b64encode(patch).decode(),
        "changed_paths": ["README.md"],
        "expires_in_seconds": expires_in_seconds,
    }


def _create_publication(
    client: TestClient, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    response = client.post("/publications", json=payload, headers=WORKER_HEADERS)
    assert response.status_code in (200, 201), response.text
    return response.status_code, response.json()


def _rows(query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(query), params or {})
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _counts() -> tuple[int, int]:
    row = _rows(
        "SELECT (SELECT count(*) FROM curie.approvals) AS approvals, "
        "(SELECT count(*) FROM curie.publications) AS publications"
    )[0]
    return int(row["approvals"]), int(row["publications"])


def _stream_entries(stream: str) -> list[Any]:
    valkey: redis.Redis = connect_or_skip(decode_responses=True)
    try:
        return list(valkey.xrange(stream))
    finally:
        valkey.close()


def _resolve(
    client: TestClient,
    auth_headers: dict[str, str],
    approval_id: str,
    *,
    decision: str = "approved",
    actor: str = "U0REQUEST1",
    channel: str = "C0EXAMPLE1",
) -> Any:
    return client.post(
        f"/approvals/{approval_id}/resolve",
        json={
            "decision": decision,
            "resolved_by": actor,
            "actor_channel": channel,
        },
        headers=auth_headers,
    )


def _assert_patch_private(value: Any, private_fragment: str) -> None:
    def keys(node: Any) -> Iterator[str]:
        if isinstance(node, dict):
            for key, child in node.items():
                yield str(key)
                yield from keys(child)
        elif isinstance(node, list):
            for child in node:
                yield from keys(child)

    rendered = json.dumps(value, sort_keys=True)
    assert {"patch", "patch_b64", "patch_bytes"}.isdisjoint(keys(value))
    assert private_fragment not in rendered


def test_publication_create_is_atomic_private_and_idempotent(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, _ = publication_stack
    deployment = _create_deployment(client, auth_headers)
    private_patch = b"private-diff-material-that-must-not-leave-the-api"
    payload = _publication_payload(
        deployment["id"], patch=private_patch, dedupe_key="event-publish-once"
    )

    first_status, first = _create_publication(client, payload)
    second_status, second = _create_publication(client, payload)

    assert first_status == 201
    assert second_status == 200
    assert second["id"] == first["id"]
    assert second["approval_id"] == first["approval_id"]
    assert first["repo_full_name"] == REPO
    assert first["status"] == "pending"
    assert first["version"] == 1
    assert _counts() == (1, 1)
    _assert_patch_private(first, base64.b64encode(private_patch).decode())

    got = client.get(f"/publications/{first['id']}", headers=auth_headers)
    listed = client.get("/publications", headers=auth_headers)
    approval = client.get(f"/approvals/{first['approval_id']}", headers=auth_headers)
    assert got.status_code == listed.status_code == approval.status_code == 200
    _assert_patch_private(got.json(), base64.b64encode(private_patch).decode())
    _assert_patch_private(listed.json(), base64.b64encode(private_patch).decode())
    _assert_patch_private(approval.json(), base64.b64encode(private_patch).decode())

    stored = _rows(
        "SELECT octet_length(patch_bytes) AS size, repo_full_name, approval_id "
        "FROM curie.publications WHERE id = :id",
        {"id": first["id"]},
    )[0]
    assert stored["size"] == len(private_patch)
    assert stored["repo_full_name"] == REPO
    assert str(stored["approval_id"]) == first["approval_id"]

    changed_replay = dict(payload)
    changed_replay["patch_b64"] = base64.b64encode(b"different patch").decode()
    conflict = client.post(
        "/publications", json=changed_replay, headers=WORKER_HEADERS
    )
    assert conflict.status_code == 409
    assert _counts() == (1, 1)


def test_publication_insert_failure_rolls_back_the_approval(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    """A real Postgres trigger fails after the Approval insert, not validation."""

    client, _ = publication_stack
    deployment = _create_deployment(client, auth_headers)

    async def install_trigger() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION curie.fail_publication_insert() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                        "RAISE EXCEPTION 'forced publication insert failure'; END $$"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TRIGGER test_fail_publication BEFORE INSERT ON "
                        "curie.publications FOR EACH ROW EXECUTE FUNCTION "
                        "curie.fail_publication_insert()"
                    )
                )
        finally:
            await engine.dispose()

    async def remove_trigger() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DROP TRIGGER IF EXISTS test_fail_publication ON "
                        "curie.publications"
                    )
                )
                await conn.execute(
                    text("DROP FUNCTION IF EXISTS curie.fail_publication_insert()")
                )
        finally:
            await engine.dispose()

    asyncio.run(install_trigger())
    try:
        with pytest.raises(Exception, match="forced publication insert failure"):
            client.post(
                "/publications",
                json=_publication_payload(deployment["id"]),
                headers=WORKER_HEADERS,
            )
    finally:
        asyncio.run(remove_trigger())
    assert _counts() == (0, 0)


@pytest.mark.parametrize(
    ("size", "expected_status"),
    [(PATCH_LIMIT - 1, 201), (PATCH_LIMIT, 201), (PATCH_LIMIT + 1, 413)],
)
def test_publication_patch_cap_counts_decoded_raw_bytes_exactly(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
    size: int,
    expected_status: int,
) -> None:
    client, _ = publication_stack
    deployment = _create_deployment(client, auth_headers)
    response = client.post(
        "/publications",
        json=_publication_payload(deployment["id"], patch=b"x" * size),
        headers=WORKER_HEADERS,
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        stored = _rows(
            "SELECT octet_length(patch_bytes) AS size FROM curie.publications"
        )
        assert stored == [{"size": size}]
        assert _counts() == (1, 1)
    else:
        assert "900000" in response.json()["detail"]
        assert _counts() == (0, 0)


def test_publication_requester_self_approval_still_requires_membership_and_is_audited(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, runs_stream = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client, _publication_payload(deployment["id"], author="U0REQUEST1")
    )

    outside = _resolve(
        client,
        auth_headers,
        publication["approval_id"],
        actor="U0REQUEST1",
        channel="C0ELSE001",
    )
    assert outside.status_code == 403
    assert "approver" in outside.json()["detail"]

    approved = _resolve(client, auth_headers, publication["approval_id"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert _stream_entries(runs_stream) == []

    stored = client.get(f"/publications/{publication['id']}", headers=auth_headers)
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == "approved"
    assert stored.json()["version"] == 2

    audit = client.get(
        f"/approvals/{publication['approval_id']}/audit", headers=auth_headers
    )
    assert audit.status_code == 200, audit.text
    denied, resolved = audit.json()
    assert denied["authorized"] is False
    assert resolved["authorized"] is True
    assert resolved["actor"] == "U0REQUEST1"
    assert resolved["evidence"]["publication_requester_exception"] is True
    assert resolved["evidence"]["approvers_channel"] == "C0EXAMPLE1"
    assert resolved["evidence"]["actor_channel"] == "C0EXAMPLE1"


def test_publication_resolution_cas_has_one_winner_and_never_enqueues_resume(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, runs_stream = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client, _publication_payload(deployment["id"])
    )

    def attempt(decision: str) -> Any:
        return _resolve(
            client,
            auth_headers,
            publication["approval_id"],
            decision=decision,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, ["approved", "rejected"]))

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert _stream_entries(runs_stream) == []
    stored = client.get(f"/publications/{publication['id']}", headers=auth_headers).json()
    winner = next(
        response.json()["status"]
        for response in responses
        if response.status_code == 200
    )
    assert stored["status"] == ("approved" if winner == "approved" else "denied")
    assert stored["version"] == 2


def test_publication_approval_is_never_in_the_owed_wake_worklist(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    """Finder, row claim, and dead-letter reopen share one exclusion."""

    client, runs_stream = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client, _publication_payload(deployment["id"])
    )
    resolved = _resolve(client, auth_headers, publication["approval_id"])
    assert resolved.status_code == 200, resolved.text

    async def inspect() -> tuple[list[uuid.UUID], bool, bool, datetime | None]:
        from curie_api.models import Approval

        engine = create_async_engine(get_settings().database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                approval_id = uuid.UUID(publication["approval_id"])
                ids = await crud.list_resolved_unresumed(
                    session,
                    resolved_before=datetime.now(UTC).replace(tzinfo=None)
                    + timedelta(days=1),
                    limit=100,
                )
                claim = await crud.claim_resume_row(session, approval_id)
                await session.rollback()
                reopened = await crud.reopen_dead_lettered_resume(
                    session,
                    approval_id,
                    dead_lettered_after=datetime.now(UTC).replace(tzinfo=None)
                    + timedelta(days=1),
                )
                approval = await session.get(Approval, approval_id)
                assert approval is not None
                return ids, claim is not None, reopened, approval.resumed_at
        finally:
            await engine.dispose()

    ids, claimed, reopened, resumed_at = asyncio.run(inspect())
    assert uuid.UUID(publication["approval_id"]) not in ids
    assert claimed is False
    assert reopened is False
    assert resumed_at is not None, "durable-only resolution must mark no wake owed"
    assert _stream_entries(runs_stream) == []


def test_expired_publication_settles_without_expiry_or_reconciler_wake(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, runs_stream = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client,
        _publication_payload(deployment["id"], expires_in_seconds=1),
    )
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)

    async def expire_and_sweep() -> tuple[int, list[uuid.UUID]]:
        engine = create_async_engine(get_settings().database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        valkey = aioredis.from_url(get_settings().valkey_dsn())
        queue = ResumeQueue(valkey, stream=runs_stream)
        try:
            async with sessionmaker() as session:
                from curie_api.models import Approval

                await session.execute(
                    update(Approval)
                    .where(Approval.id == uuid.UUID(publication["approval_id"]))
                    .values(expires_at=past)
                )
                await session.commit()
                count = await sweep_expired_approvals(
                    session, queue, now=past + timedelta(seconds=1)
                )
                ids = await crud.list_resolved_unresumed(
                    session,
                    resolved_before=past + timedelta(days=1),
                    limit=100,
                )
                return count, ids
        finally:
            await valkey.aclose()
            await engine.dispose()

    count, owed = asyncio.run(expire_and_sweep())
    assert count == 1
    assert uuid.UUID(publication["approval_id"]) not in owed
    assert _stream_entries(runs_stream) == []
    assert client.get(
        f"/approvals/{publication['approval_id']}", headers=auth_headers
    ).json()["status"] == "expired"
    assert client.get(
        f"/publications/{publication['id']}", headers=auth_headers
    ).json()["status"] == "expired"


def test_denied_publication_has_no_credential_or_launch_opportunity(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, runs_stream = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client, _publication_payload(deployment["id"])
    )

    denied = _resolve(
        client,
        auth_headers,
        publication["approval_id"],
        decision="rejected",
    )
    assert denied.status_code == 200, denied.text
    stored = client.get(f"/publications/{publication['id']}", headers=auth_headers)
    assert stored.json()["status"] == "denied"
    assert stored.json()["version"] == 2
    credential = client.post(
        f"/publications/{publication['id']}/credential", headers=WORKER_HEADERS
    )
    assert credential.status_code == 409
    assert credential.headers["cache-control"] == "no-store"
    assert "approved" in credential.json()["detail"].lower()
    assert _stream_entries(runs_stream) == []
    assert _rows(
        "SELECT count(*) AS count FROM curie.publications "
        "WHERE status IN ('approved', 'launching', 'running')"
    ) == [{"count": 0}]


def test_publication_credential_is_approved_only_server_derived_and_audited(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, _ = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, publication = _create_publication(
        client, _publication_payload(deployment["id"])
    )
    url = f"/publications/{publication['id']}/credential"

    pending = client.post(url, headers=WORKER_HEADERS)
    assert pending.status_code == 409
    assert pending.headers["cache-control"] == "no-store"
    platform_key = client.post(url, headers=auth_headers)
    assert platform_key.status_code == 401
    assert platform_key.headers["cache-control"] == "no-store"

    approved = _resolve(client, auth_headers, publication["approval_id"])
    assert approved.status_code == 200, approved.text
    issued = client.post(
        url,
        json={"repo_full_name": "attacker/other-bot"},
        headers=WORKER_HEADERS,
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"
    assert issued.json() == {
        "clone_url": "https://github.com/acme-corp/acme-bot.git",
        "token": "ghp_publication_operator",
    }

    audit = _rows(
        "SELECT purpose, outcome, deployment_id, publication_id, repo_full_name, "
        "detail FROM curie.credential_redemption_audit_entries "
        "WHERE publication_id = :id ORDER BY created_at, id",
        {"id": publication["id"]},
    )
    assert [row["outcome"] for row in audit] == ["refused", "refused", "issued"]
    assert all(row["purpose"] == "publication_push" for row in audit)
    assert all(str(row["publication_id"]) == publication["id"] for row in audit)
    assert all(str(row["deployment_id"]) == deployment["id"] for row in audit)
    assert all(row["repo_full_name"] == REPO for row in audit)
    assert "ghp_publication_operator" not in json.dumps(audit, default=str)


def test_terminal_patch_retention_reaps_bytes_but_keeps_public_metadata(
    publication_stack: tuple[TestClient, str],
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    client, _ = publication_stack
    deployment = _create_deployment(client, auth_headers)
    _, terminal = _create_publication(
        client,
        _publication_payload(
            deployment["id"],
            patch=b"terminal-private-patch",
            dedupe_key="terminal-publication",
        ),
    )
    _, pending = _create_publication(
        client,
        _publication_payload(
            deployment["id"],
            patch=b"pending-private-patch",
            dedupe_key="pending-publication",
        ),
    )
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)

    async def terminal_and_reap() -> int:
        from curie_api.models import Publication

        engine = create_async_engine(get_settings().database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                await session.execute(
                    update(Publication)
                    .where(Publication.id == uuid.UUID(terminal["id"]))
                    .values(
                        status="succeeded",
                        terminal_at=old,
                        result_url="https://github.com/acme-corp/acme-bot/pull/123",
                        version=Publication.version + 1,
                    )
                )
                await session.commit()
                return await crud.reap_terminal_publication_patches(
                    session,
                    terminal_before=old + timedelta(minutes=1),
                    limit=100,
                )
        finally:
            await engine.dispose()

    assert asyncio.run(terminal_and_reap()) == 1
    sizes = _rows(
        "SELECT id, octet_length(patch_bytes) AS size FROM curie.publications "
        "ORDER BY id"
    )
    by_id = {str(row["id"]): row["size"] for row in sizes}
    assert by_id[terminal["id"]] is None
    assert by_id[pending["id"]] == len(b"pending-private-patch")

    read = client.get(f"/publications/{terminal['id']}", headers=auth_headers)
    assert read.status_code == 200, read.text
    assert read.json()["status"] == "succeeded"
    assert read.json()["result_url"].endswith("/pull/123")
    _assert_patch_private(read.json(), "terminal-private-patch")
