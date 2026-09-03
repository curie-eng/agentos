"""GitHub publication recovery adopts only the exact approved pull request."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from urllib.parse import quote

import httpx
import pytest
from channel_protocol import scoped_conversation_id
from curie_worker.publication_clients import (
    GitHubPublicationLookup,
    PublicationTranscriptClient,
)
from curie_worker.publication_loop import (
    PublicationReconcileError,
    PublicationTranscriptPermanentError,
)

REPO = "acme-corp/acme-bot"
BRANCH = "curie/publication-22222222222242228222222222222222"
TITLE = "Update repository"
BODY = "Approved platform publication."
PR_URL = f"https://github.com/{REPO}/pull/123"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _pull(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "html_url": PR_URL,
        "title": TITLE,
        "body": BODY,
        "head": {"ref": BRANCH, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    row.update(overrides)
    return row


def _transport(pull: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == f"/repos/{REPO}/pulls":
            return httpx.Response(200, json=[pull])
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def test_recovery_adopts_exact_approved_pull_request() -> None:
    async with httpx.AsyncClient(transport=_transport(_pull())) as client:
        lookup = GitHubPublicationLookup(client)

        recovered = await lookup.recover_pr_by_head(
            REPO,
            BRANCH,
            TITLE,
            BODY,
            "Bearer operator-token",
        )

    assert recovered == PR_URL


async def test_publication_result_is_appended_once_to_the_durable_transcript() -> None:
    publication_id = "22222222-2222-4222-8222-222222222222"
    agent_id = "11111111-1111-4111-8111-111111111111"
    appends: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "platform-key"
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "POST"
        assert request.url.path.endswith("/append")
        appends.append(json.loads(request.content))
        return httpx.Response(200, json={"value": [appends[-1]["item"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        await transcript.record_result(
            uuid.UUID(agent_id),
            "1700000000.000100",
            uuid.UUID(publication_id),
            f"Published the approved changes: {PR_URL}",
        )

    assert len(appends) == 1
    item = appends[0]["item"]
    assert isinstance(item, dict)
    assert item["publication_id"] == publication_id
    assert item["assistant"] == f"Published the approved changes: {PR_URL}"


async def test_transcript_path_quotes_the_raw_canonical_workspace_identity_once() -> None:
    publication_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    agent_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    canonical = scoped_conversation_id(
        "slack:socket",
        "C0EXAMPLE1/alerts",
        "1700000000.000100%followup",
    )
    encoded = quote(canonical, safe="")
    paths: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.url.path, request.url.raw_path))
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={"value": [json.loads(request.content)["item"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        await transcript.record_result(
            agent_id,
            canonical,
            publication_id,
            f"Published the approved changes: {PR_URL}",
        )

    expected_path = f"/agents/{agent_id}/state/transcript/{encoded}"
    assert paths == [
        (f"/agents/{agent_id}/state/transcript/{canonical}", expected_path.encode()),
        (
            f"/agents/{agent_id}/state/transcript/{canonical}/append",
            f"{expected_path}/append".encode(),
        ),
    ]


async def test_existing_publication_transcript_record_is_not_appended_again() -> None:
    publication_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "value": [{"publication_id": str(publication_id)}],
                "version": 4,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        await transcript.record_result(
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            "1700000000.000100",
            publication_id,
            f"Published the approved changes: {PR_URL}",
        )

    assert calls == ["GET"]


async def test_atomic_append_never_replaces_a_preexisting_transcript_item() -> None:
    publication_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    prior: dict[str, object] = {
        "user": "Earlier turn",
        "assistant": "Earlier answer",
    }
    stored: list[dict[str, object]] = [prior]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"value": list(stored), "version": 7})
        assert request.method == "POST"
        assert request.url.path.endswith("/append")
        body = json.loads(request.content)
        assert set(body) == {"item"}
        stored.append(body["item"])
        return httpx.Response(200, json={"value": list(stored), "version": 8})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        await transcript.record_result(
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            "1700000000.000100",
            publication_id,
            f"Published the approved changes: {PR_URL}",
        )

    assert calls == ["GET", "POST"]
    assert stored[0] == prior
    assert len(stored) == 2
    assert stored[1]["publication_id"] == str(publication_id)


async def test_lost_append_response_is_absorbed_by_recovery_get() -> None:
    publication_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    stored: list[dict[str, object]] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            if not stored:
                return httpx.Response(404)
            return httpx.Response(200, json={"value": list(stored), "version": 1})
        assert request.method == "POST"
        assert request.url.path.endswith("/append")
        body = json.loads(request.content)
        stored.append(body["item"])
        raise httpx.ReadError("append response was lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        await transcript.record_result(
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            "1700000000.000100",
            publication_id,
            f"Published the approved changes: {PR_URL}",
        )

    assert calls == ["GET", "POST", "GET"]
    assert len(stored) == 1
    assert stored[0]["publication_id"] == str(publication_id)


async def test_transcript_capacity_refusal_is_classified_as_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"value": [], "version": 7})
        assert request.method == "POST"
        return httpx.Response(413)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transcript = PublicationTranscriptClient(
            api_base_url="https://api.example.com",
            api_key="platform-key",
            client=client,
        )
        with pytest.raises(
            PublicationTranscriptPermanentError,
            match="exceeded durable state capacity",
        ):
            await transcript.record_result(
                uuid.UUID("11111111-1111-4111-8111-111111111111"),
                "1700000000.000100",
                uuid.UUID("22222222-2222-4222-8222-222222222222"),
                f"Published the approved changes: {PR_URL}",
            )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(title="Different title"),
        lambda row: row.update(body="Different body"),
        lambda row: row.update(head={"ref": "other", "repo": {"full_name": REPO}}),
        lambda row: row.update(
            head={"ref": BRANCH, "repo": {"full_name": "acme-corp/other"}}
        ),
        lambda row: row.update(base={"ref": "other", "repo": {"full_name": REPO}}),
        lambda row: row.update(
            base={"ref": "main", "repo": {"full_name": "acme-corp/other"}}
        ),
    ],
    ids=("title", "body", "head-ref", "head-repo", "base-ref", "base-repo"),
)
async def test_recovery_rejects_mutated_pull_request_contract(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    row = _pull()
    mutate(row)
    async with httpx.AsyncClient(transport=_transport(row)) as client:
        lookup = GitHubPublicationLookup(client)

        with pytest.raises(PublicationReconcileError, match="contract"):
            await lookup.recover_pr_by_head(
                REPO,
                BRANCH,
                TITLE,
                BODY,
                "Bearer operator-token",
            )


async def test_create_response_is_validated_before_reporting_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path.endswith(f"/git/ref/heads/{BRANCH}"):
            return httpx.Response(200, json={"ref": f"refs/heads/{BRANCH}"})
        if request.url.path == f"/repos/{REPO}/pulls" and request.method == "GET":
            calls += 1
            return httpx.Response(200, json=[])
        if request.url.path == f"/repos/{REPO}/pulls" and request.method == "POST":
            assert json.loads(request.content) == {
                "title": TITLE,
                "head": BRANCH,
                "base": "main",
                "body": BODY,
            }
            return httpx.Response(201, json=_pull(title="Mutated after creation"))
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        lookup = GitHubPublicationLookup(client)
        with pytest.raises(PublicationReconcileError, match="contract"):
            await lookup.recover_pr_by_head(
                REPO,
                BRANCH,
                TITLE,
                BODY,
                "Bearer operator-token",
            )

    assert calls == 1
