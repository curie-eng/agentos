"""Approval decisions reconcile to one publication Job and a direct routed result."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from channel_protocol.reply import ReplyTarget
from curie_worker.approval_cards import ApprovalCardRef
from curie_worker.reply_sink import TargetRoute

PUBLICATION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
APPROVAL_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
PR_URL = "https://github.com/acme-corp/acme-bot/pull/123"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def publication() -> Any:
    return importlib.import_module("curie_worker.publication_loop")


class _Store:
    def __init__(self) -> None:
        self.completed: dict[uuid.UUID, tuple[str, str | None]] = {}
        self.failures: list[tuple[uuid.UUID, str]] = []
        self.pending: dict[uuid.UUID, Any] = {}
        self.delivered: set[uuid.UUID] = set()
        self.retries: list[tuple[uuid.UUID, str]] = []
        self.delivery_retries: list[tuple[uuid.UUID, str]] = []
        self.target = _target()
        self.route = TargetRoute(endpoint=None, adapter=None)
        self.retry_terminal_after = 99

    def is_terminal(self, publication_id: uuid.UUID) -> bool:
        return publication_id in self.completed

    def complete(self, publication_id: uuid.UUID, *, outcome: str, pr_url: str | None) -> None:
        self.completed[publication_id] = (outcome, pr_url)

    def fail(self, publication_id: uuid.UUID, *, error: str) -> None:
        self.failures.append((publication_id, error))

    def pending_result(self, publication_id: uuid.UUID | None = None) -> Any | None:
        if publication_id is None:
            publication_id = next(iter(self.pending), None)
        if publication_id is None:
            return None
        value = self.pending.get(publication_id)
        if value is None:
            return None
        return SimpleNamespace(
            publication_id=publication_id,
            approval_id=APPROVAL_ID,
            target=self.target,
            route=self.route,
            attempt=1,
            version=1,
            **value,
        )

    def persist_result(
        self,
        publication_id: uuid.UUID,
        *,
        outcome: str,
        pr_url: str | None,
        error: str | None,
    ) -> None:
        self.completed[publication_id] = (outcome, pr_url)
        if outcome == "failed" and error is not None:
            self.failures.append((publication_id, error))
        self.pending[publication_id] = {
            "outcome": outcome,
            "pr_url": pr_url,
            "error": error,
        }

    def mark_result_delivered(self, publication_id: uuid.UUID) -> None:
        self.delivered.add(publication_id)
        self.pending.pop(publication_id, None)

    def retry_result_delivery(self, publication_id: uuid.UUID, *, error: str) -> None:
        self.delivery_retries.append((publication_id, error))

    def retry(self, publication_id: uuid.UUID, *, error: str) -> None:
        self.retries.append((publication_id, error))
        if len(self.retries) >= self.retry_terminal_after:
            self.completed[publication_id] = ("failed", None)
            self.failures.append((publication_id, error))
            self.pending[publication_id] = {
                "outcome": "failed",
                "pr_url": None,
                "error": error,
            }

    async def claim_next(self) -> None:
        return None


class _Credentials:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.calls: list[uuid.UUID] = []
        self.error: Exception | None = None

    def redeem(self, publication_id: uuid.UUID) -> Any:
        self.calls.append(publication_id)
        if self.error is not None:
            raise self.error
        return self.module.PublicationCredential(
            clean_clone_url="https://github.com/acme-corp/acme-bot.git",
            authorization_header="Basic publication-write-credential-value",
        )


class _Cluster:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.applied: list[Any] = []
        self.credentials_cleaned: list[Any] = []
        self.terminals_cleaned: list[Any] = []
        self.observation = module.PublicationJobObservation(
            phase="succeeded", pr_url=PR_URL, logs=f"CURIE_PR_URL={PR_URL}\n"
        )
        self.preexisting_observation: Any | None = None
        self.raise_after_apply = False

    def apply(self, resources: Any) -> None:
        self.applied.append(resources)
        if self.raise_after_apply:
            self.raise_after_apply = False
            raise RuntimeError("worker stopped after apiserver accepted resources")

    def observe(self, job_name: str) -> Any:
        if not self.applied:
            return self.preexisting_observation or self.module.PublicationJobObservation(
                phase="pending", pr_url=None, logs="", exists=False
            )
        return self.observation

    def cleanup(self, names: Any) -> None:
        self.credentials_cleaned.append(names)
        self.terminals_cleaned.append(names)

    def cleanup_credentials(self, names: Any) -> None:
        self.credentials_cleaned.append(names)

    def cleanup_terminal(self, names: Any) -> None:
        self.terminals_cleaned.append(names)


class _GitHub:
    def __init__(self, recovered: str | None = None) -> None:
        self.recovered = recovered
        self.calls: list[tuple[str, str, str, str, str]] = []

    def recover_pr_by_head(
        self,
        repo_full_name: str,
        branch: str,
        title: str,
        body: str,
        authorization_header: str,
    ) -> str | None:
        self.calls.append((repo_full_name, branch, title, body, authorization_header))
        return self.recovered


class _Replies:
    def __init__(self) -> None:
        self.events: list[tuple[Any, TargetRoute]] = []
        self.fail_once = False
        self.on_emit: Any | None = None

    async def emit(
        self, event: Any, *, route: TargetRoute, best_effort_unreachable: bool = False
    ) -> Any:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("reply transport unavailable")
        self.events.append((event, route))
        if self.on_emit is not None:
            self.on_emit()
        return object()


class _Cards:
    def __init__(self) -> None:
        self.ref: ApprovalCardRef | None = None
        self.popped: list[str] = []
        self.restored: list[tuple[str, ApprovalCardRef]] = []

    async def pop(self, thread: str) -> ApprovalCardRef | None:
        self.popped.append(thread)
        ref, self.ref = self.ref, None
        return ref

    async def restore(self, thread: str, ref: ApprovalCardRef) -> None:
        self.restored.append((thread, ref))
        if self.ref is None:
            self.ref = ref


def _card(*, approval_id: str = str(APPROVAL_ID)) -> ApprovalCardRef:
    return ApprovalCardRef(
        channel="C0EXAMPLE1",
        ts="1700000000.000050",
        summary="Publish these repository changes?",
        requested_by="requester@example.test",
        approval_id=approval_id,
        kind="slack",
    )


def _target(kind: str = "slack") -> ReplyTarget:
    return ReplyTarget(
        kind=kind,
        address="C0EXAMPLE1" if kind == "slack" else "agent@example.test",
        conversation_id="1700000000.000100",
        reply_ref=None,
    )


def _work(module: Any, *, decision: str = "approved", kind: str = "slack") -> Any:
    return module.PublicationWork(
        publication_id=PUBLICATION_ID,
        approval_id=APPROVAL_ID,
        decision=decision,
        repo_full_name="acme-corp/acme-bot",
        base_sha="a" * 40,
        patch=b"diff --git a/README.md b/README.md\n",
        changed_paths=("README.md",),
        title="Update repository",
        body="Approved platform publication.",
        target=_target(kind),
        route=TargetRoute(
            endpoint=None if kind == "slack" else "https://adapter.example.com/replies",
            adapter=None if kind == "slack" else "agentmail-sandbox",
        ),
        version=1,
    )


def _loop(
    module: Any,
    cards: _Cards | None = None,
) -> tuple[Any, _Store, _Credentials, _Cluster, _GitHub, _Replies]:
    k8s = importlib.import_module("curie_worker.publication_k8s")
    store = _Store()
    credentials = _Credentials(module)
    cluster = _Cluster(module)
    github = _GitHub()
    replies = _Replies()
    cards = cards or _Cards()
    loop = module.PublicationReconciler(
        store=store,
        credentials=credentials,
        cluster=cluster,
        github=github,
        replies=replies,
        card_store=cards,
        job_settings=k8s.PublicationJobSettings(
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
        ),
    )
    return loop, store, credentials, cluster, github, replies


async def test_approved_publication_launches_job_and_reports_pr_url(
    publication: Any,
) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    work = _work(publication)

    await loop.reconcile(work)

    assert credentials.calls == [PUBLICATION_ID]
    assert len(cluster.applied) == 1
    resources = cluster.applied[0]
    assert resources.job["kind"] == "Job"
    assert PR_URL not in str(resources.secret), "the result is learned from the Job"
    assert len(github.calls) == 1
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert cluster.credentials_cleaned == [resources.names]
    assert cluster.terminals_cleaned == [resources.names]
    assert len(replies.events) == 1
    event, route = replies.events[0]
    assert event.target == work.target
    assert PR_URL in event.text
    assert route == work.route
    assert not hasattr(loop, "runner") and not hasattr(loop, "model")


async def test_denied_publication_does_not_create_job_or_push(publication: Any) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    work = _work(publication, decision="denied")

    await loop.reconcile(work)

    assert credentials.calls == []
    assert cluster.applied == []
    assert cluster.credentials_cleaned == []
    assert cluster.terminals_cleaned == []
    assert github.calls == []
    assert store.completed == {PUBLICATION_ID: ("denied", None)}
    assert len(replies.events) == 1
    assert "not published" in replies.events[0][0].text.lower()
    assert "push" in replies.events[0][0].text.lower()


async def test_worker_crash_reuses_the_same_job_and_cannot_duplicate_publication(
    publication: Any,
) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    work = _work(publication)
    cluster.raise_after_apply = True

    with pytest.raises(RuntimeError, match="worker stopped"):
        await loop.reconcile(work)
    await loop.reconcile(work)
    await loop.reconcile(work)  # terminal duplicate callback is a no-op

    job_names = [resources.names.job for resources in cluster.applied]
    assert len(set(job_names)) == 1
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert len(replies.events) == 1
    assert len(github.calls) == 1


async def test_existing_remote_pr_is_adopted_before_recreating_a_missing_job(
    publication: Any,
) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    github.recovered = PR_URL
    work = _work(publication)

    await loop.reconcile(work)

    branch = publication.deterministic_publication_branch(PUBLICATION_ID)
    assert github.calls == [
        (
            "acme-corp/acme-bot",
            branch,
            "Update repository",
            "Approved platform publication.",
            "Basic publication-write-credential-value",
        )
    ]
    assert cluster.applied == [], "remote adoption must happen before a replacement Job"
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert PR_URL in replies.events[0][0].text


async def test_non_slack_publication_result_uses_the_stored_adapter_route_without_model(
    publication: Any,
) -> None:
    loop, store, _, _, _, replies = _loop(publication)
    work = _work(publication, kind="email")
    store.target = work.target
    store.route = work.route

    await loop.reconcile(work)

    event, route = replies.events[0]
    assert event.target.kind == "email"
    assert route == TargetRoute(
        endpoint="https://adapter.example.com/replies", adapter="agentmail-sandbox"
    )
    assert PR_URL in event.text
    assert store.completed[PUBLICATION_ID] == ("published", PR_URL)
    assert not hasattr(loop, "runner") and not hasattr(loop, "model")


@pytest.mark.parametrize(
    ("outcome", "pr_url", "error", "decision", "text_fragment"),
    [
        ("published", PR_URL, None, "approved", "Published the approved changes"),
        ("denied", None, None, "rejected", "request was denied"),
        ("failed", None, "push failed", "approved", "failed safely after approval"),
        ("expired", None, None, None, "approval expired"),
    ],
)
async def test_terminal_result_settles_matching_approval_card(
    publication: Any,
    outcome: str,
    pr_url: str | None,
    error: str | None,
    decision: str | None,
    text_fragment: str,
) -> None:
    cards = _Cards()
    loop, store, _, _, _, replies = _loop(publication, cards)
    cards.ref = _card()
    store.pending[PUBLICATION_ID] = {
        "outcome": outcome,
        "pr_url": pr_url,
        "error": error,
    }

    await loop.deliver_pending_result(PUBLICATION_ID)

    assert text_fragment in replies.events[0][0].text
    card_update, card_route = replies.events[1]
    assert card_update.target.reply_ref == "1700000000.000050"
    assert card_update.message.text == "Publish these repository changes?"
    assert card_update.settled.decision == decision
    assert card_update.settled.requested_by == "requester@example.test"
    assert card_route == TargetRoute(endpoint=None, adapter=None)
    assert cards.ref is None
    assert cards.restored == []


async def test_mismatched_approval_card_is_restored_without_wrong_settlement(
    publication: Any,
) -> None:
    cards = _Cards()
    loop, store, _, _, _, replies = _loop(publication, cards)
    mismatched = _card(approval_id="44444444-4444-4444-8444-444444444444")
    cards.ref = mismatched
    store.pending[PUBLICATION_ID] = {
        "outcome": "published",
        "pr_url": PR_URL,
        "error": None,
    }

    await loop.deliver_pending_result(PUBLICATION_ID)

    assert cards.ref == mismatched
    assert cards.restored == [(_target().conversation_id, mismatched)]
    assert len(replies.events) == 1
    assert replies.events[0][0].settled is None
    assert PUBLICATION_ID in store.delivered


async def test_only_exact_approved_decision_can_redeem_write_credential(
    publication: Any,
) -> None:
    for decision in ("denied", "expired", "pending"):
        loop, _, credentials, cluster, github, _ = _loop(publication)
        await loop.reconcile(_work(publication, decision=decision))
        assert credentials.calls == [], decision
        assert cluster.applied == [], decision
        assert github.calls == [], decision


async def test_terminal_result_is_persisted_and_credentials_removed_before_reply_retry(
    publication: Any,
) -> None:
    cards = _Cards()
    loop, store, credentials, cluster, _, replies = _loop(publication, cards)
    replies.fail_once = True
    work = _work(publication)
    cards.ref = _card()

    with pytest.raises(RuntimeError, match="reply transport unavailable"):
        await loop.reconcile(work)

    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert PUBLICATION_ID in store.pending
    assert len(cluster.credentials_cleaned) == 1
    assert cluster.terminals_cleaned == []
    assert credentials.calls == [PUBLICATION_ID]
    assert cards.ref == _card()
    assert cards.restored == [(_target().conversation_id, _card())]

    await loop.deliver_pending_result(PUBLICATION_ID)

    assert PUBLICATION_ID in store.delivered
    assert store.pending == {}
    assert len(cluster.credentials_cleaned) == 2
    assert len(cluster.terminals_cleaned) == 1
    assert credentials.calls == [PUBLICATION_ID]
    assert len(replies.events) == 2
    assert replies.events[1][0].settled.decision == "approved"
    assert cards.ref is None
    assert store.delivery_retries == [(PUBLICATION_ID, "reply transport unavailable")]


async def test_supervisor_drains_terminal_result_outbox_without_job_work(
    publication: Any,
) -> None:
    reconciler, store, _, cluster, _, replies = _loop(publication)
    store.completed[PUBLICATION_ID] = ("published", PR_URL)
    store.pending[PUBLICATION_ID] = {
        "outcome": "published",
        "pr_url": PR_URL,
        "error": None,
    }
    shutdown = asyncio.Event()
    replies.on_emit = shutdown.set
    supervisor = publication.PublicationReconcileLoop(
        store=store,
        reconciler=reconciler,
        interval_seconds=0.01,
    )

    await supervisor.run_forever(shutdown)

    assert PUBLICATION_ID in store.delivered
    assert len(cluster.credentials_cleaned) == 1
    assert len(cluster.terminals_cleaned) == 1
    assert PR_URL in replies.events[0][0].text


@pytest.mark.parametrize("phase", ["failed", "succeeded"])
async def test_terminal_job_recovers_remote_branch_or_lost_rest_response_before_failure(
    publication: Any,
    phase: str,
) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    github.recovered = PR_URL
    cluster.preexisting_observation = publication.PublicationJobObservation(
        phase=phase,
        pr_url=None,
        logs="publication process exited without a marker\n",
        error="job process exited" if phase == "failed" else None,
    )

    await loop.reconcile(_work(publication))

    assert credentials.calls == [PUBLICATION_ID]
    assert len(github.calls) == 1
    assert cluster.applied == []
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert PR_URL in replies.events[0][0].text


async def test_credential_setup_failure_is_bounded_and_terminalized(publication: Any) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    credentials.error = publication.PublicationReconcileError(
        "publication credential endpoint is unreachable"
    )
    work = _work(publication)
    store.retry_terminal_after = 2

    await loop.reconcile(work)
    assert store.retries == [
        (PUBLICATION_ID, "publication credential endpoint is unreachable")
    ]
    assert store.completed == {}
    assert replies.events == []

    await loop.reconcile(work)
    assert store.completed == {PUBLICATION_ID: ("failed", None)}
    assert store.failures == [
        (PUBLICATION_ID, "publication credential endpoint is unreachable")
    ]
    assert cluster.applied == []
    assert github.calls == []
    assert "failed safely" in replies.events[0][0].text.lower()
