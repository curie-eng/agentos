"""Approval decisions reconcile to one publication Job and a direct routed result."""

from __future__ import annotations

import importlib
import uuid
from typing import Any

import pytest
from channel_protocol.reply import ReplyTarget
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

    def is_terminal(self, publication_id: uuid.UUID) -> bool:
        return publication_id in self.completed

    def complete(self, publication_id: uuid.UUID, *, outcome: str, pr_url: str | None) -> None:
        self.completed[publication_id] = (outcome, pr_url)

    def fail(self, publication_id: uuid.UUID, *, error: str) -> None:
        self.failures.append((publication_id, error))


class _Credentials:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.calls: list[uuid.UUID] = []

    def redeem(self, publication_id: uuid.UUID) -> Any:
        self.calls.append(publication_id)
        return self.module.PublicationCredential(
            clean_clone_url="https://github.com/acme-corp/acme-bot.git",
            authorization_header="Basic publication-write-credential-value",
        )


class _Cluster:
    def __init__(self, module: Any) -> None:
        self.module = module
        self.applied: list[Any] = []
        self.cleaned: list[Any] = []
        self.observation = module.PublicationJobObservation(
            phase="succeeded", pr_url=PR_URL, logs=f"CURIE_PR_URL={PR_URL}\n"
        )
        self.raise_after_apply = False

    def apply(self, resources: Any) -> None:
        self.applied.append(resources)
        if self.raise_after_apply:
            self.raise_after_apply = False
            raise RuntimeError("worker stopped after apiserver accepted resources")

    def observe(self, job_name: str) -> Any:
        return self.observation

    def cleanup(self, names: Any) -> None:
        self.cleaned.append(names)


class _GitHub:
    def __init__(self, recovered: str | None = None) -> None:
        self.recovered = recovered
        self.calls: list[tuple[str, str]] = []

    def find_pr_by_head(self, repo_full_name: str, branch: str) -> str | None:
        self.calls.append((repo_full_name, branch))
        return self.recovered


class _Replies:
    def __init__(self) -> None:
        self.events: list[tuple[Any, TargetRoute]] = []

    async def emit(
        self, event: Any, *, route: TargetRoute, best_effort_unreachable: bool = False
    ) -> Any:
        self.events.append((event, route))
        return object()


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


def _loop(module: Any) -> tuple[Any, _Store, _Credentials, _Cluster, _GitHub, _Replies]:
    k8s = importlib.import_module("curie_worker.publication_k8s")
    store = _Store()
    credentials = _Credentials(module)
    cluster = _Cluster(module)
    github = _GitHub()
    replies = _Replies()
    loop = module.PublicationReconciler(
        store=store,
        credentials=credentials,
        cluster=cluster,
        github=github,
        replies=replies,
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
    assert github.calls == []
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert cluster.cleaned == [resources.names]
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
    assert cluster.cleaned == []
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
    assert github.calls == []


async def test_missing_marker_recovers_existing_pr_by_deterministic_head(
    publication: Any,
) -> None:
    loop, store, credentials, cluster, github, replies = _loop(publication)
    github.recovered = PR_URL
    cluster.observation = publication.PublicationJobObservation(
        phase="succeeded", pr_url=None, logs="publish completed without marker\n"
    )
    work = _work(publication)

    await loop.reconcile(work)

    branch = publication.deterministic_publication_branch(PUBLICATION_ID)
    assert github.calls == [("acme-corp/acme-bot", branch)]
    assert len({resources.names.job for resources in cluster.applied}) == 1
    assert store.completed == {PUBLICATION_ID: ("published", PR_URL)}
    assert PR_URL in replies.events[0][0].text


async def test_non_slack_publication_result_uses_the_stored_adapter_route_without_model(
    publication: Any,
) -> None:
    loop, store, _, _, _, replies = _loop(publication)
    work = _work(publication, kind="email")

    await loop.reconcile(work)

    event, route = replies.events[0]
    assert event.target.kind == "email"
    assert route == TargetRoute(
        endpoint="https://adapter.example.com/replies", adapter="agentmail-sandbox"
    )
    assert PR_URL in event.text
    assert store.completed[PUBLICATION_ID] == ("published", PR_URL)
    assert not hasattr(loop, "runner") and not hasattr(loop, "model")


async def test_only_exact_approved_decision_can_redeem_write_credential(
    publication: Any,
) -> None:
    for decision in ("denied", "expired", "pending"):
        loop, _, credentials, cluster, github, _ = _loop(publication)
        await loop.reconcile(_work(publication, decision=decision))
        assert credentials.calls == [], decision
        assert cluster.applied == [], decision
        assert github.calls == [], decision
