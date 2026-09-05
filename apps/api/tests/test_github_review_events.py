"""GitHub sender fixtures through the feedback-normalization boundary.

Payload families and action names are documented at
https://docs.github.com/en/webhooks/webhook-events-and-payloads#issue_comment
and the pull_request_review / pull_request_review_comment sections. These
fixtures prove parsing and refusal only; HMAC, current GitHub truth and durable
lineage authorization are exercised separately through the actual HTTP ingress.
"""

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import socket
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest
from channel_protocol import scoped_conversation_id
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from curie_api.config import Settings, get_settings
from curie_api.github_app import _RESOLVERS
from curie_api.github_review_events import FeedbackIgnored, parse_feedback
from curie_api.github_review_truth import BoundReviewLineage, verify_feedback_truth
from curie_api.main import create_app
from curie_test_support.valkey import connect_or_skip
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DELIVERY = str(uuid.UUID(int=1))
REPO = "acme-corp/acme-bot"
HEAD = "a" * 40


def feedback_payload(event: str = "issue_comment") -> dict:
    user = {"id": 41, "login": "example-reviewer", "type": "User"}
    feedback = {
        "id": 71,
        "body": "Please add a regression test before updating this PR.",
        "user": user,
        "created_at": "2026-09-05T01:00:00Z",
        "updated_at": "2026-09-05T01:00:00Z",
        "performed_via_github_app": None,
        "author_association": "MEMBER",
    }
    result = {
        "action": "created",
        "installation": {"id": 11},
        "repository": {"id": 21, "full_name": REPO},
        "sender": copy.deepcopy(user),
    }
    pr = {
        "number": 17,
        "state": "open",
        "html_url": f"https://github.com/{REPO}/pull/17",
        "head": {"sha": HEAD, "ref": "curie/example", "repo": {"id": 21, "full_name": REPO}},
        "base": {"repo": {"id": 21, "full_name": REPO}},
    }
    if event == "issue_comment":
        result["issue"] = {
            "number": 17,
            "state": "open",
            "pull_request": {
                "html_url": pr["html_url"],
                "url": f"https://api.github.com/repos/{REPO}/pulls/17",
            },
        }
        feedback["html_url"] = f"{pr['html_url']}#issuecomment-71"
        result["comment"] = feedback
    elif event == "pull_request_review_comment":
        feedback.update(
            {
                "html_url": f"{pr['html_url']}#discussion_r71",
                "commit_id": HEAD,
                "path": "src/example.py",
                "line": 12,
                "pull_request_review_id": 81,
            }
        )
        result.update({"pull_request": pr, "comment": feedback})
    else:
        feedback.update(
            {
                "html_url": f"{pr['html_url']}#pullrequestreview-71",
                "commit_id": HEAD,
                "state": "changes_requested",
                "submitted_at": "2026-09-05T01:00:00Z",
            }
        )
        result.update({"action": "submitted", "pull_request": pr, "review": feedback})
    return result


@pytest.mark.parametrize(
    "event", ["issue_comment", "pull_request_review_comment", "pull_request_review"]
)
def test_each_human_review_family_retains_canonical_identity_and_provenance(event: str) -> None:
    result = parse_feedback(event, feedback_payload(event), DELIVERY)
    assert result.repo_full_name == REPO
    assert result.pr_number == 17
    assert result.sender_id == 41 and result.sender_login == "example-reviewer"
    assert result.feedback_id == 71 and result.installation_id == 11
    assert result.body == "Please add a regression test before updating this PR."
    assert result.url.startswith(f"https://github.com/{REPO}/pull/17#")
    assert (
        result.event_id
        == parse_feedback(event, feedback_payload(event), str(uuid.UUID(int=2))).event_id
    )
    if event == "pull_request_review_comment":
        assert result.path == "src/example.py" and result.line == 12 and result.review_id == 81


@pytest.mark.parametrize("action", ["edited", "deleted", "dismissed"])
def test_non_creation_actions_are_observably_ignored(action: str) -> None:
    payload = feedback_payload()
    payload["action"] = action
    with pytest.raises(FeedbackIgnored, match="unsupported_action"):
        parse_feedback("issue_comment", payload, DELIVERY)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p.pop("installation"), "invalid_installation"),
        (lambda p: p["installation"].update(id=True), "invalid_installation"),
        (lambda p: p["repository"].update(full_name="acme-corp/../other"), "invalid_repository"),
        (lambda p: p["sender"].update(id=42), "sender_mismatch"),
        (lambda p: p["sender"].update(type="Bot"), "non_human_sender"),
        (lambda p: p["sender"].update(login="ghost"), "non_human_sender"),
        (lambda p: p["comment"]["user"].update(type="Bot"), "non_human_sender"),
        (lambda p: p["comment"].update(performed_via_github_app={"id": 51}), "app_authored"),
        (lambda p: p["issue"].pop("pull_request"), "not_pull_request"),
        (lambda p: p["issue"].update(state="closed"), "terminal_pull_request"),
        (lambda p: p["comment"].update(body="  "), "empty_feedback"),
        (
            lambda p: p["comment"].update(html_url="https://evil.example.com/private-sentinel"),
            "invalid_feedback_url",
        ),
        (lambda p: p["comment"].update(updated_at="2026-09-05T01:00:01Z"), "edited_feedback"),
    ],
)
def test_invalid_or_non_human_feedback_cannot_be_normalized(mutation, reason: str) -> None:
    payload = feedback_payload()
    mutation(payload)
    with pytest.raises(FeedbackIgnored, match=reason) as caught:
        parse_feedback("issue_comment", payload, DELIVERY)
    assert "private-sentinel" not in str(caught.value)


def test_delivery_header_must_be_a_real_uuid() -> None:
    with pytest.raises(FeedbackIgnored, match="invalid_delivery"):
        parse_feedback("issue_comment", feedback_payload(), "not-a-delivery-private-sentinel")


def test_app_reinstallation_does_not_change_feedback_execution_identity() -> None:
    payload = feedback_payload()
    original = parse_feedback("issue_comment", payload, DELIVERY)
    payload["installation"]["id"] = 12
    replacement = parse_feedback("issue_comment", payload, str(uuid.UUID(int=3)))
    assert replacement.event_id == original.event_id
    assert replacement.installation_id != original.installation_id


@pytest.mark.parametrize("association", [None, "NONE", "CONTRIBUTOR", "FIRST_TIMER", [], "member"])
def test_drive_by_or_malformed_sender_association_cannot_authorize_feedback(association) -> None:
    payload = feedback_payload()
    payload["comment"]["author_association"] = association
    with pytest.raises(FeedbackIgnored, match="unauthorized_association"):
        parse_feedback("issue_comment", payload, DELIVERY)


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_signed_member_associations_are_retained_and_verified(association: str) -> None:
    payload = feedback_payload()
    payload["comment"]["author_association"] = association
    assert parse_feedback("issue_comment", payload, DELIVERY).author_association == association


def test_review_ingress_is_disabled_by_default_and_refuses_incomplete_enablement() -> None:
    from pydantic import ValidationError

    assert Settings().github_review_ingress_enabled is False
    with pytest.raises(ValidationError, match="GitHub review ingress"):
        Settings(github_review_ingress_enabled=True)


@pytest.mark.parametrize("state", [None, 1, {}, [], "approved", "dismissed"])
def test_non_actionable_or_malformed_review_state_has_a_redacted_refusal(state) -> None:
    payload = feedback_payload("pull_request_review")
    payload["review"]["state"] = state
    with pytest.raises(FeedbackIgnored, match="non_actionable_review"):
        parse_feedback("pull_request_review", payload, DELIVERY)


class GitHubTruth:
    """Only GitHub HTTP is replaced; signer/resolver/verifier execute normally.

    REST comment identities, issue_url and pull_request_url follow:
    https://docs.github.com/en/rest/issues/comments#get-an-issue-comment
    https://docs.github.com/en/rest/pulls/comments#get-a-review-comment-for-a-pull-request
    https://docs.github.com/en/rest/pulls/reviews#get-a-review-for-a-pull-request
    """

    def __init__(self, event: str, key: str):
        self.payload = feedback_payload(event)
        self.feedback = parse_feedback(event, self.payload, DELIVERY)
        self.settings = Settings(
            github_app_id="51",
            github_app_private_key=key,
            github_token="fixture-pat-must-not-be-used",
        )
        self.lineage = BoundReviewLineage(REPO, 17, "curie/example", HEAD)
        self.calls: list[str] = []
        self.installation = {"id": 11}
        self.installation_status = 200
        self.repo = {"id": 21, "full_name": REPO}
        self.pr = copy.deepcopy(feedback_payload("pull_request_review")["pull_request"])
        self.pr["merged"] = False
        self.comment = copy.deepcopy(self.payload.get("comment", self.payload.get("review")))
        self.comment["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/17"
        self.comment["pull_request_url"] = f"https://api.github.com/repos/{REPO}/pulls/17"
        self.feedback_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        assert "fixture-pat" not in request.headers.get("Authorization", "")
        if request.url.path.endswith("/installation"):
            return httpx.Response(self.installation_status, json=self.installation)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "fixture-app-token-private-sentinel",
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            )
        assert request.headers["Authorization"] == "Bearer fixture-app-token-private-sentinel"
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(200, json=self.repo)
        if request.url.path == f"/repos/{REPO}/pulls/17":
            return httpx.Response(200, json=self.pr)
        return httpx.Response(
            self.feedback_status,
            json=self.comment,
            headers={"Location": "https://evil.example.com/private-sentinel"},
        )


@pytest.fixture(scope="module")
def review_app_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


async def verify_truth(truth: GitHubTruth, monkeypatch: pytest.MonkeyPatch) -> str:
    real_client = httpx.Client
    monkeypatch.setattr(
        "curie_api.github_app.httpx.Client",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(truth.handle)),
    )
    _RESOLVERS.clear()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(truth.handle), follow_redirects=True
        ) as client:
            return await verify_feedback_truth(
                truth.feedback,
                truth.lineage,
                settings=truth.settings,
                client=client,
            )
    finally:
        _RESOLVERS.clear()


@pytest.mark.parametrize(
    "event", ["issue_comment", "pull_request_review_comment", "pull_request_review"]
)
def test_current_app_repo_pr_and_human_feedback_must_independently_agree(
    event: str,
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = GitHubTruth(event, review_app_key)
    assert asyncio.run(verify_truth(truth, monkeypatch)) == HEAD
    assert truth.calls[0] == f"/repos/{REPO}/installation"
    assert f"/repos/{REPO}" in truth.calls
    assert f"/repos/{REPO}/pulls/17" in truth.calls


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda t: t.installation.update(id=12), "installation_unverified"),
        (lambda t: setattr(t, "installation_status", 302), "installation_unverified"),
        (lambda t: t.repo.update(id=22), "repository_mismatch"),
        (lambda t: t.pr["head"].update(ref="other-branch"), "pull_request_mismatch"),
        (lambda t: t.pr["head"].update(sha="b" * 40), "stale_feedback_head"),
        (lambda t: t.pr["base"]["repo"].update(id=22), "repository_mismatch"),
        (lambda t: t.pr.update(state="closed", merged=True), "terminal_pull_request"),
        (
            lambda t: t.comment.update(body="forged-current-body-private-sentinel"),
            "feedback_changed",
        ),
        (lambda t: t.comment["user"].update(id=42), "feedback_changed"),
        (lambda t: t.comment.update(updated_at="2026-09-05T01:00:01Z"), "edited_feedback"),
        (
            lambda t: t.comment.update(issue_url=f"https://api.github.com/repos/{REPO}/issues/18"),
            "feedback_target_mismatch",
        ),
        (lambda t: setattr(t, "feedback_status", 404), "feedback_unavailable"),
        (lambda t: setattr(t, "feedback_status", 401), "feedback_unavailable"),
        (lambda t: setattr(t, "feedback_status", 302), "feedback_unavailable"),
    ],
)
def test_signed_claims_cannot_override_current_github_authority(
    mutation,
    reason: str,
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = GitHubTruth("issue_comment", review_app_key)
    mutation(truth)
    with pytest.raises(FeedbackIgnored, match=reason) as caught:
        asyncio.run(verify_truth(truth, monkeypatch))
    assert "private-sentinel" not in str(caught.value)
    assert not any("private-sentinel" in path for path in truth.calls)


def test_a_user_pat_is_not_product_app_installation_proof(
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = GitHubTruth("issue_comment", review_app_key)
    truth.settings = Settings(github_token="fixture-pat-must-not-be-used")
    with pytest.raises(FeedbackIgnored, match="installation_unverified"):
        asyncio.run(verify_truth(truth, monkeypatch))
    assert truth.calls == []


def test_webhook_repo_cannot_select_a_different_persisted_lineage(
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth = GitHubTruth("issue_comment", review_app_key)
    truth.lineage = replace(truth.lineage, repo_full_name="acme-corp/other-bot")
    with pytest.raises(FeedbackIgnored, match="lineage_mismatch"):
        asyncio.run(verify_truth(truth, monkeypatch))
    assert truth.calls == []


@pytest.mark.parametrize("surface", ["installation", "feedback"])
@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_current_authority_outage_is_retryable_and_recovers(
    surface: str,
    status: int,
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curie_api.github_review_events import FeedbackUnavailable

    truth = GitHubTruth("issue_comment", review_app_key)
    # GitHub hides private resources behind 404 and uses 403 for rate limits.
    # https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api
    setattr(truth, f"{surface}_status", status)
    with pytest.raises(FeedbackUnavailable):
        asyncio.run(verify_truth(truth, monkeypatch))
    setattr(truth, f"{surface}_status", 200)
    assert asyncio.run(verify_truth(truth, monkeypatch)) == HEAD


def test_verified_reinstallation_retires_old_cached_token_before_any_repo_read(
    review_app_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curie_api.github_app import GitHubCredentials

    truth = GitHubTruth("issue_comment", review_app_key)
    requests: list[httpx.Request] = []

    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            installation = request.url.path.split("/")[-2]
            return httpx.Response(
                201,
                json={
                    "token": f"fixture-installation-{installation}",
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            )
        return truth.handle(request)

    real_client = httpx.Client
    monkeypatch.setattr(
        "curie_api.github_app.httpx.Client",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(github)),
    )
    credentials = GitHubCredentials(truth.settings)
    assert credentials.token_for_verified_installation(REPO, 11) == "fixture-installation-11"
    # An unchanged installation may reuse its cache; a different installation
    # must mint a new token even when the previous token has not yet expired.
    assert credentials.token_for_verified_installation(REPO, 11) == "fixture-installation-11"
    truth.installation["id"] = 12
    assert credentials.token_for_verified_installation(REPO, 12) == "fixture-installation-12"
    assert [r.url.path for r in requests if r.method == "POST"] == [
        "/app/installations/11/access_tokens",
        "/app/installations/12/access_tokens",
    ]


def review_rows(statement: str, parameters: dict | None = None) -> list[dict]:
    async def execute() -> list[dict]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), parameters or {})
                return [dict(row) for row in result.mappings()] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(execute())


@pytest.fixture
def review_stack(
    clean_db: None,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    review_app_key: str,
) -> Iterator[tuple[TestClient, GitHubTruth, object, str]]:
    """Real migrated Postgres/Valkey/API, with only GitHub HTTP replaced.

    The database seed represents a PR already published by #2274. Setting that
    historical fixture is not an exercised approval or GitHub publication.
    """
    event = getattr(request, "param", "issue_comment")
    truth = GitHubTruth(event, review_app_key)
    stream = f"test:curie:github-review:{uuid.uuid4().hex}"
    for key, value in {
        "RUNS_STREAM": stream,
        "INTERNAL_WORKER_TOKEN": "fixture-review-worker-token",
        "GITHUB_WEBHOOK_SECRET": "fixture-review-webhook-secret",
        "GITHUB_REVIEW_INGRESS_ENABLED": "true",
        "GITHUB_APP_ID": "51",
        "GITHUB_APP_PRIVATE_KEY": review_app_key,
        "GITHUB_TOKEN": "",
        "GITHUB_REPO_ALLOWLIST": '["acme-corp/*"]',
        "GITHUB_REVIEW_RECONCILER_INTERVAL_S": "0",
        "APPROVAL_SWEEP_INTERVAL_S": "0",
        "RESUME_RECONCILER_ENABLED": "false",
        "DEAD_LETTER_WATCH_INTERVAL_S": "0",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    _RESOLVERS.clear()
    valkey = connect_or_skip(decode_responses=True)
    with ExitStack() as owned:
        owned.callback(get_settings.cache_clear)
        owned.callback(_RESOLVERS.clear)
        owned.callback(valkey.close)
        owned.callback(
            valkey.delete,
            stream,
            f"{stream}:dead",
            f"curie:github-review:{truth.feedback.event_id}",
        )
        client = owned.enter_context(TestClient(create_app()))
        real_client = httpx.Client
        monkeypatch.setattr(
            "curie_api.github_app.httpx.Client",
            lambda *a, **kw: real_client(transport=httpx.MockTransport(truth.handle)),
        )
        external = httpx.AsyncClient(transport=httpx.MockTransport(truth.handle))
        owned.callback(client.portal.call, external.aclose)
        client.app.state.http_client = external
        auth = {"X-API-Key": get_settings().api_key}
        agent = client.post(
            "/agents",
            headers=auth,
            json={
                "name": f"acme-review-{uuid.uuid4().hex[:8]}",
                "repo_full_name": REPO,
                "channel": {"kind": "slack", "address": "C0EXAMPLE1"},
            },
        )
        assert agent.status_code == 201, agent.text
        agent_id = agent.json()["id"]
        version = client.post(
            f"/agents/{agent_id}/versions",
            headers=auth,
            json={"version_label": "fixture", "created_by": "operator"},
        )
        assert version.status_code == 201, version.text
        deployment = client.post(
            "/deployments",
            headers=auth,
            json={
                "agent_id": agent_id,
                "version_id": version.json()["id"],
                "environment": "dev",
            },
        )
        assert deployment.status_code == 201, deployment.text
        selected = client.post(
            f"/v1/internal/workspaces/{deployment.json()['id']}/selection",
            headers={"X-Curie-Worker-Token": "fixture-review-worker-token"},
            json={
                "conversation_id": scoped_conversation_id(
                    "slack", "C0EXAMPLE1", "1700000000.000001"
                ),
                "author": "U0REQUEST1",
                "repo_full_name": REPO,
            },
        )
        assert selected.status_code == 200, selected.text
        publication = client.post(
            "/v1/internal/publications",
            headers={
                "X-Curie-Worker-Token": "fixture-review-worker-token",
            },
            json={
                "deployment_id": deployment.json()["id"],
                "conversation_id": "1700000000.000001",
                "repo_full_name": REPO,
                "author": "U0REQUEST1",
                "summary": "Fixture publication",
                "reply_kind": "slack",
                "reply_channel": "C0EXAMPLE1",
                "reply_placeholder": "1700000000.000002",
                "dedupe_key": f"fixture-{uuid.uuid4()}",
                "base_sha": HEAD,
                "patch_b64": base64.b64encode(b"diff --git a/a b/a\n").decode(),
                "changed_paths": ["a"],
                "expires_in_seconds": 600,
            },
        )
        assert publication.status_code == 201, publication.text
        lineage_id = publication.json()["lineage_id"]
        branch = publication.json()["branch"]
        truth.pr["head"]["ref"] = branch
        review_rows(
            "UPDATE curie.thread_publication_lineages SET head_sha=:head, pr_number=17, "
            "pr_url=:url, version=2 WHERE id=:id",
            {"head": HEAD, "url": f"https://github.com/{REPO}/pull/17", "id": lineage_id},
        )
        review_rows(
            "UPDATE curie.publications SET status='succeeded', outcome_history_ready_at=now(), "
            "result_reported_at=now(), terminal_at=now() WHERE id=:id",
            {"id": publication.json()["id"]},
        )
        yield client, truth, valkey, stream


def post_review(
    client: TestClient,
    truth: GitHubTruth,
    *,
    delivery: str = DELIVERY,
    signature: str | None = None,
) -> httpx.Response:
    body = json.dumps(truth.payload).encode()
    signature = (
        signature
        or "sha256="
        + hmac.new(
            b"fixture-review-webhook-secret",
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": truth.feedback.event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )


@pytest.mark.parametrize(
    "review_stack",
    [
        "issue_comment",
        "pull_request_review_comment",
        "pull_request_review",
    ],
    indirect=True,
)
def test_real_ingress_persists_and_enqueues_exactly_one_honest_bound_turn(review_stack) -> None:
    from aci_protocol import parse_queued_turn

    client, truth, valkey, stream = review_stack
    first = post_review(client, truth)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "feedback_queued"
    for delivery in (DELIVERY, str(uuid.UUID(int=2))):
        duplicate = post_review(client, truth, delivery=delivery)
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["status"] == "feedback_duplicate"
    entries = valkey.xrange(stream)
    assert len(entries) == 1
    turn = parse_queued_turn(entries[0][1]["payload"])
    assert turn.event_id == truth.feedback.event_id
    assert turn.conversation_id == "1700000000.000001"
    assert turn.reply_handle.kind == "slack" and turn.reply_handle.channel == "C0EXAMPLE1"
    assert turn.reply_handle.placeholder is None
    assert turn.author == "github:41:example-reviewer"
    assert not turn.source.is_job
    assert truth.feedback.body in turn.text and truth.feedback.url in turn.text
    assert "fixture-review-worker-token" not in entries[0][1]["payload"]
    assert "fixture-app-token" not in entries[0][1]["payload"]
    rows = review_rows("SELECT status, stream_id, version FROM curie.github_review_feedback")
    assert rows == [{"status": "queued", "stream_id": entries[0][0], "version": 2}]


def test_invalid_hmac_cannot_read_github_persist_or_enqueue(review_stack) -> None:
    client, truth, valkey, stream = review_stack
    response = post_review(client, truth, signature="sha256=invalid")
    assert response.status_code == 401
    assert truth.calls == []
    assert valkey.xlen(stream) == 0
    assert review_rows("SELECT event_id FROM curie.github_review_feedback") == []


def test_disabled_review_ingress_does_not_read_authority_or_enqueue(
    review_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, truth, valkey, stream = review_stack
    monkeypatch.setenv("GITHUB_REVIEW_INGRESS_ENABLED", "false")
    get_settings.cache_clear()
    response = post_review(client, truth)
    assert response.status_code == 200 and response.json()["status"] == "feedback_disabled"
    assert truth.calls == []
    assert review_rows("SELECT event_id FROM curie.github_review_feedback") == []
    assert valkey.xlen(stream) == 0


def test_binding_quota_refuses_new_feedback_and_keeps_an_observable_row(review_stack) -> None:
    client, truth, valkey, stream = review_stack
    client.app.state.github_review_reconciler._settings.channel_binding_backlog_limit = 0
    response = post_review(client, truth)
    assert response.status_code == 200 and response.json()["status"] == "feedback_refused"
    assert valkey.xlen(stream) == 0
    rows = review_rows("SELECT status,error_code FROM curie.github_review_feedback")
    assert rows == [{"status": "refused", "error_code": "binding_backlog_quota"}]
    assert post_review(client, truth).json()["status"] == "feedback_duplicate"
    assert valkey.xlen(stream) == 0


def test_real_enqueue_refusal_backs_off_then_recovers_without_second_quota(review_stack) -> None:
    import redis.asyncio as redis

    client, truth, valkey, stream = review_stack
    username = f"review-test-{uuid.uuid4().hex}"
    password = "fixture-owned-valkey-acl-token"
    # Only this task-created ACL identity loses XADD. The backing server and
    # other clients remain healthy; no Postgres/Valkey service is mocked.
    valkey.execute_command("ACL", "SETUSER", username, "on", f">{password}", "~*", "+@all", "-xadd")
    restricted = redis.Redis.from_url(
        str(httpx.URL(get_settings().valkey_dsn()).copy_with(username=username, password=password)),
        decode_responses=True,
    )
    reconciler = client.app.state.github_review_reconciler
    original = reconciler._valkey
    reconciler._valkey = restricted
    try:
        assert client.portal.call(restricted.ping) is True
        response = post_review(client, truth)
        assert response.json()["status"] == "feedback_waiting"
        row = review_rows(
            "SELECT enqueue_attempts,quota_taken,next_attempt_at FROM curie.github_review_feedback"
        )[0]
        assert row["enqueue_attempts"] == 1 and row["quota_taken"] is True
        assert row["next_attempt_at"] is not None and valkey.xlen(stream) == 0
        assert client.portal.call(reconciler.reconcile_once) == 0
        assert (
            review_rows("SELECT enqueue_attempts FROM curie.github_review_feedback")[0][
                "enqueue_attempts"
            ]
            == 1
        )
        # Queue several real scans behind one DB connection. They can all read
        # the due candidate before the first locked attempt updates its retry
        # deadline; later locks must recheck that deadline rather than burn it.
        review_rows("UPDATE curie.github_review_feedback SET next_attempt_at=NULL")

        async def competing_passes() -> list[int]:
            from curie_api.github_review_store import GitHubReviewReconciler
            from sqlalchemy.ext.asyncio import async_sessionmaker

            engine = create_async_engine(get_settings().database_url, pool_size=1, max_overflow=0)
            try:
                contender = GitHubReviewReconciler(
                    async_sessionmaker(engine, expire_on_commit=False), restricted, get_settings()
                )
                return await asyncio.gather(*(contender.reconcile_once() for _ in range(4)))
            finally:
                await engine.dispose()

        assert client.portal.call(competing_passes) == [0, 0, 0, 0]
        assert (
            review_rows("SELECT enqueue_attempts FROM curie.github_review_feedback")[0][
                "enqueue_attempts"
            ]
            == 2
        )
        valkey.execute_command("ACL", "SETUSER", username, "+xadd")
        review_rows(
            "UPDATE curie.github_review_feedback SET next_attempt_at=now()-interval '1 second'"
        )
        assert client.portal.call(reconciler.reconcile_once) == 1
        assert valkey.xlen(stream) == 1
        assert post_review(client, truth).json()["status"] == "feedback_duplicate"
        assert valkey.xlen(stream) == 1
    finally:
        reconciler._valkey = original
        client.portal.call(restricted.aclose)
        valkey.execute_command("ACL", "DELUSER", username)


def test_signed_feedback_with_wrong_current_head_cannot_create_a_turn(review_stack) -> None:
    client, truth, valkey, stream = review_stack
    truth.pr["head"]["sha"] = "b" * 40
    response = post_review(client, truth)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "feedback_ignored"
    assert response.json()["errors"] == [{"code": "stale_feedback_head"}]
    assert valkey.xlen(stream) == 0
    assert review_rows("SELECT event_id FROM curie.github_review_feedback") == []


def test_before_model_revalidation_rejects_edit_and_never_accepts_platform_key(
    review_stack,
) -> None:
    client, truth, valkey, stream = review_stack
    assert post_review(client, truth).json()["status"] == "feedback_queued"
    turn = json.loads(valkey.xrange(stream)[0][1]["payload"])
    deployment = review_rows("SELECT deployment_id FROM curie.thread_publication_lineages")[0]
    payload = {"turn": turn, "deployment_id": str(deployment["deployment_id"])}
    path = f"/v1/internal/github/reviews/{truth.feedback.event_id}/verify"
    unauthenticated = client.post(path, json=payload, headers={"X-API-Key": get_settings().api_key})
    assert unauthenticated.status_code == 401
    headers = {"X-Curie-Worker-Token": "fixture-review-worker-token"}
    verified = client.post(path, json=payload, headers=headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["head_sha"] == HEAD
    assert verified.json()["sender"] == "github:41:example-reviewer"
    truth.comment["body"] = "edited-private-sentinel"
    rejected = client.post(path, json=payload, headers=headers)
    assert rejected.status_code == 409
    assert "private-sentinel" not in rejected.text
    assert valkey.xlen(stream) == 1


def test_production_worker_client_uses_actual_api_without_github_or_slack_authority(
    review_stack,
) -> None:
    from aci_protocol import parse_queued_turn
    from curie_worker.approvals import ApprovalBackendError, ApprovalClient

    client, truth, valkey, stream = review_stack
    assert post_review(client, truth).json()["status"] == "feedback_queued"
    turn = parse_queued_turn(valkey.xrange(stream)[0][1]["payload"])
    deployment_id = review_rows("SELECT deployment_id FROM curie.thread_publication_lineages")[0][
        "deployment_id"
    ]

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://api.example.test",
        ) as http:
            worker = ApprovalClient(
                api_base_url="http://api.example.test",
                api_key="",
                client=http,
                read_timeout_s=2,
                worker_token="fixture-review-worker-token",
            )
            verified = await worker.verify_review_feedback(turn, deployment_id)
            assert verified.head_sha == HEAD
            assert verified.sender == "github:41:example-reviewer"
            assert truth.feedback.url in verified.receipt
            truth.feedback_status = 404
            with pytest.raises(ApprovalBackendError):
                await worker.verify_review_feedback(turn, deployment_id)

    client.portal.call(exercise)


def test_review_verification_uses_its_own_budget_over_actual_api_http(review_stack) -> None:
    """The production card-read budget is too short for fresh GitHub reads.

    Uvicorn serves the actual API with real Postgres/Valkey; only external GitHub
    responses are controlled. ASGITransport does not enforce HTTP timeouts, so
    this regression deliberately uses a loopback socket and a delayed provider.
    """
    import uvicorn
    from aci_protocol import parse_queued_turn
    from curie_worker.approvals import ApprovalBackendError, ApprovalClient

    client, truth, valkey, stream = review_stack
    assert post_review(client, truth).json()["status"] == "feedback_queued"
    turn = parse_queued_turn(valkey.xrange(stream)[0][1]["payload"])
    deployment_id = review_rows("SELECT deployment_id FROM curie.thread_publication_lineages")[0][
        "deployment_id"
    ]

    async def exercise() -> None:
        async def delayed_github(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/comments/71"):
                await asyncio.sleep(2.2)
            return truth.handle(request)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        server = uvicorn.Server(
            uvicorn.Config(client.app, lifespan="off", log_level="critical", access_log=False)
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        previous = client.app.state.http_client
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(delayed_github)) as github,
                httpx.AsyncClient() as http,
            ):
                client.app.state.http_client = github
                worker = ApprovalClient(
                    api_base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                    api_key="",
                    client=http,
                    read_timeout_s=2.0,
                    worker_token="fixture-review-worker-token",
                )
                assert (await worker.verify_review_feedback(turn, deployment_id)).head_sha == HEAD
                # A mixed-token rollout is infrastructure unavailability and
                # cannot become a terminal policy refusal that ACKs the turn.
                wrong_token = ApprovalClient(
                    api_base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                    api_key="",
                    client=http,
                    read_timeout_s=2.0,
                    worker_token="fixture-old-worker-token",
                )
                with pytest.raises(ApprovalBackendError):
                    await wrong_token.verify_review_feedback(turn, deployment_id)
        finally:
            client.app.state.http_client = previous
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 5)
            finally:
                sock.close()

    client.portal.call(exercise)


def test_real_queue_receipt_survives_database_commit_failure_without_second_turn(
    review_stack,
) -> None:
    from sqlalchemy.exc import DBAPIError

    client, truth, valkey, stream = review_stack
    # Task-owned disposable DB fault: the real outbox XADD succeeds, then the
    # real transaction fails while flushing its queued mark. No service mocked.
    review_rows("""
        CREATE FUNCTION curie.review_test_reject_queued() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.status = 'queued' THEN RAISE EXCEPTION 'task-owned queued-mark failure'; END IF;
          RETURN NEW;
        END $$
    """)
    review_rows("""
        CREATE TRIGGER review_test_reject_queued BEFORE UPDATE ON curie.github_review_feedback
        FOR EACH ROW EXECUTE FUNCTION curie.review_test_reject_queued()
    """)
    try:
        with pytest.raises(DBAPIError):
            post_review(client, truth)
        assert valkey.xlen(stream) == 1
        assert review_rows("SELECT status, version FROM curie.github_review_feedback") == [
            {"status": "waiting", "version": 1}
        ]
    finally:
        review_rows("DROP TRIGGER review_test_reject_queued ON curie.github_review_feedback")
        review_rows("DROP FUNCTION curie.review_test_reject_queued()")
    assert client.portal.call(client.app.state.github_review_reconciler.reconcile_once) == 1
    assert client.portal.call(client.app.state.github_review_reconciler.reconcile_once) == 0
    assert valkey.xlen(stream) == 1
    assert review_rows("SELECT status, version FROM curie.github_review_feedback") == [
        {"status": "queued", "version": 2}
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE curie.agent_channels SET generation=generation+1",
        "UPDATE curie.thread_publication_lineages SET version=version+1",
    ],
)
def test_before_model_recheck_refuses_stale_binding_or_lineage_version(
    review_stack, mutation
) -> None:
    client, truth, valkey, stream = review_stack
    assert post_review(client, truth).json()["status"] == "feedback_queued"
    turn = json.loads(valkey.xrange(stream)[0][1]["payload"])
    deployment_id = review_rows("SELECT deployment_id FROM curie.thread_publication_lineages")[0][
        "deployment_id"
    ]
    review_rows(mutation)
    response = client.post(
        f"/v1/internal/github/reviews/{truth.feedback.event_id}/verify",
        json={"turn": turn, "deployment_id": str(deployment_id)},
        headers={"X-Curie-Worker-Token": "fixture-review-worker-token"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "binding_or_lineage_changed"
    assert valkey.xlen(stream) == 1


def test_concurrent_distinct_delivery_headers_for_one_feedback_enqueue_once(review_stack) -> None:
    client, truth, valkey, stream = review_stack
    barrier = threading.Barrier(4)

    def deliver(index: int) -> tuple[int, str]:
        barrier.wait(timeout=10)
        response = post_review(client, truth, delivery=str(uuid.UUID(int=100 + index)))
        return response.status_code, response.json()["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, range(4)))
    assert sorted(results) == [(200, "feedback_duplicate")] * 3 + [(200, "feedback_queued")]
    assert valkey.xlen(stream) == 1
    assert len(review_rows("SELECT event_id FROM curie.github_review_feedback")) == 1


def test_ambiguous_persisted_pr_cannot_choose_a_conversation_from_webhook_contents(
    review_stack,
) -> None:
    client, truth, valkey, stream = review_stack
    review_rows(
        """
        INSERT INTO curie.thread_publication_lineages (
          id,agent_id,deployment_id,conversation_id,repo_full_name,base_sha,branch,
          pr_number,pr_url,head_sha,status,version,latest_revision
        ) SELECT :id,agent_id,deployment_id,'other-conversation',repo_full_name,base_sha,
          'curie/other-conversation',pr_number,pr_url,head_sha,status,version,latest_revision
          FROM curie.thread_publication_lineages
    """,
        {"id": uuid.uuid4()},
    )
    response = post_review(client, truth)
    assert response.status_code == 200, response.text
    assert response.json()["errors"] == [{"code": "lineage_absent_or_ambiguous"}]
    assert truth.calls == []
    assert valkey.xlen(stream) == 0


def test_forged_slack_principal_on_queued_github_feedback_is_refused_by_actual_api(
    review_stack,
) -> None:
    client, truth, valkey, stream = review_stack
    assert post_review(client, truth).json()["status"] == "feedback_queued"
    turn = json.loads(valkey.xrange(stream)[0][1]["payload"])
    turn["author"] = "U0REQUEST1"
    deployment_id = review_rows("SELECT deployment_id FROM curie.thread_publication_lineages")[0][
        "deployment_id"
    ]
    response = client.post(
        f"/v1/internal/github/reviews/{truth.feedback.event_id}/verify",
        json={"turn": turn, "deployment_id": str(deployment_id)},
        headers={"X-Curie-Worker-Token": "fixture-review-worker-token"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "feedback_turn_mismatch"
    assert valkey.xlen(stream) == 1
