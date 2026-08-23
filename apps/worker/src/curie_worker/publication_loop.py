"""Worker-owned reconciliation of approval-gated repository publications."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from channel_protocol.reply import REPLY_WIRE_VERSION, ReplyTarget, ReplyUpdate

from .publication_k8s import (
    PublicationJobSettings,
    PublicationPayload,
    PublicationResourceNames,
    build_publication_resources,
    deterministic_publication_branch,
)
from .reply_sink import ReplySink, TargetRoute

_PR_MARKER = re.compile(r"^CURIE_PR_URL=(https://github\.com/[^\s]+/pull/\d+)$", re.MULTILINE)
logger = logging.getLogger(__name__)


class PublicationReconcileError(RuntimeError):
    """A durable publication could not reach a safe terminal state."""


class LocalPublicationUnsupported(PublicationReconcileError):
    """Publication was requested on the local substrate, which v1 refuses."""


@dataclass(frozen=True)
class PublicationCredential:
    clean_clone_url: str
    authorization_header: str


@dataclass(frozen=True)
class PublicationJobObservation:
    phase: str
    pr_url: str | None
    logs: str
    error: str | None = None


@dataclass(frozen=True)
class PublicationWork:
    publication_id: uuid.UUID
    approval_id: uuid.UUID
    decision: str
    repo_full_name: str
    base_sha: str
    patch: bytes
    changed_paths: tuple[str, ...]
    title: str
    body: str
    target: ReplyTarget
    route: TargetRoute
    version: int


class PublicationStore(Protocol):
    def is_terminal(self, publication_id: uuid.UUID) -> bool | Awaitable[bool]: ...

    def complete(
        self, publication_id: uuid.UUID, *, outcome: str, pr_url: str | None
    ) -> None | Awaitable[None]: ...

    def fail(self, publication_id: uuid.UUID, *, error: str) -> None | Awaitable[None]: ...


class PublicationCredentialSource(Protocol):
    def redeem(
        self, publication_id: uuid.UUID
    ) -> PublicationCredential | Awaitable[PublicationCredential]: ...


class PublicationCluster(Protocol):
    def apply(self, resources: Any) -> None | Awaitable[None]: ...

    def observe(
        self, job_name: str
    ) -> PublicationJobObservation | Awaitable[PublicationJobObservation]: ...

    def cleanup(self, names: PublicationResourceNames) -> None | Awaitable[None]: ...


class PublicationGitHub(Protocol):
    def find_pr_by_head(
        self, repo_full_name: str, branch: str
    ) -> str | None | Awaitable[str | None]: ...


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _marker_url(logs: str) -> str | None:
    match = _PR_MARKER.search(logs)
    return match.group(1) if match else None


class PublicationReconciler:
    """Converge one durable decision onto one deterministic Job and result."""

    def __init__(
        self,
        *,
        store: PublicationStore,
        credentials: PublicationCredentialSource,
        cluster: PublicationCluster,
        github: PublicationGitHub,
        replies: ReplySink,
        job_settings: PublicationJobSettings,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._cluster = cluster
        self._github = github
        self._replies = replies
        self._job_settings = job_settings

    async def _report(self, work: PublicationWork, text: str) -> None:
        await self._replies.emit(
            ReplyUpdate(
                version=REPLY_WIRE_VERSION,
                event="reply.update",
                target=work.target,
                text=text,
            ),
            route=work.route,
            best_effort_unreachable=False,
        )

    async def reconcile(self, work: PublicationWork) -> None:
        if await _resolve(self._store.is_terminal(work.publication_id)):
            return

        if work.decision == "denied":
            await self._report(
                work,
                "Changes were not published: the publication request was denied, so no "
                "credential was redeemed and no branch was pushed.",
            )
            await _resolve(self._store.complete(work.publication_id, outcome="denied", pr_url=None))
            return

        # Pending, expired, and unknown states are never authority. Expiry is
        # terminalized by the API/store lane; it still creates no cluster or
        # GitHub side effect here.
        if work.decision != "approved":
            return

        credential = await _resolve(self._credentials.redeem(work.publication_id))
        branch = deterministic_publication_branch(work.publication_id)
        resources = build_publication_resources(
            PublicationPayload(
                publication_id=work.publication_id,
                repo_full_name=work.repo_full_name,
                clean_clone_url=credential.clean_clone_url,
                base_sha=work.base_sha,
                patch=work.patch,
                branch=branch,
                title=work.title,
                body=work.body,
            ),
            credential=credential.authorization_header,
            settings=self._job_settings,
        )

        try:
            # Server-side apply/create-or-adopt must use these deterministic
            # names. A crash immediately after the apiserver accepts this call
            # therefore re-adopts the same Job on the next lease.
            await _resolve(self._cluster.apply(resources))
            observation = await _resolve(self._cluster.observe(resources.names.job))
            if observation.phase in {"pending", "running"}:
                return
            if observation.phase != "succeeded":
                raise PublicationReconcileError(
                    observation.error or "publication Job failed without a diagnostic"
                )

            pr_url = observation.pr_url or _marker_url(observation.logs)
            if pr_url is None:
                # A pod/log response can disappear after GitHub accepted the
                # request. Deterministic-head adoption separates that case from
                # a failure and prevents a duplicate pull request.
                authenticated_lookup = getattr(self._github, "find_authenticated_pr_by_head", None)
                if authenticated_lookup is not None:
                    pr_url = await _resolve(
                        authenticated_lookup(
                            work.repo_full_name,
                            branch,
                            credential.authorization_header,
                        )
                    )
                else:
                    pr_url = await _resolve(
                        self._github.find_pr_by_head(work.repo_full_name, branch)
                    )
            if pr_url is None:
                raise PublicationReconcileError(
                    "publication Job succeeded but no pull request was found for its branch"
                )

            await self._report(work, f"Published the approved changes: {pr_url}")
            await _resolve(
                self._store.complete(work.publication_id, outcome="published", pr_url=pr_url)
            )
        except Exception as exc:
            # A process-crash simulation deliberately raises immediately after
            # apply. At that point we cannot know whether the Job is live, so do
            # not mark the row failed or delete the idempotency anchor; let the
            # durable lease expire and re-adopt it.
            if not isinstance(exc, PublicationReconcileError):
                raise
            await self._report(work, f"Publication failed safely: {exc}")
            await _resolve(self._store.fail(work.publication_id, error=str(exc)))
        finally:
            # A terminal store method may race another reconciler. Cleanup is
            # idempotent and happens only after a terminal observation/failure,
            # never while the Job is pending/running or after an ambiguous apply.
            terminal = await _resolve(self._store.is_terminal(work.publication_id))
            if terminal:
                await _resolve(self._cluster.cleanup(resources.names))


class PublicationReconcileLoop:
    """Poll durable work under the worker's ordinary task supervisor."""

    def __init__(
        self,
        *,
        store: Any,
        reconciler: PublicationReconciler,
        interval_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("publication reconciliation interval must be positive")
        self._store = store
        self._reconciler = reconciler
        self._interval = interval_seconds

    async def run_forever(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            work = await self._store.claim_next()
            if work is not None:
                try:
                    await self._reconciler.reconcile(work)
                except Exception:
                    # The lease is intentionally left in place. A worker crash
                    # or ambiguous apiserver response is retried only after it
                    # expires, adopting the deterministic resource names.
                    logger.exception(
                        "publication reconciliation failed publication_id=%s",
                        work.publication_id,
                    )
                    continue
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self._interval)
            except TimeoutError:
                pass


__all__ = [
    "LocalPublicationUnsupported",
    "PublicationCredential",
    "PublicationJobObservation",
    "PublicationReconcileError",
    "PublicationReconciler",
    "PublicationReconcileLoop",
    "PublicationWork",
    "deterministic_publication_branch",
]
