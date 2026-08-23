"""Worker-owned reconciliation of approval-gated repository publications."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from channel_protocol import MESSAGE_VERSION, OutboundMessage
from channel_protocol.reply import (
    REPLY_WIRE_VERSION,
    ReplyTarget,
    ReplyUpdate,
    SettledOutcome,
)

from .approval_cards import ApprovalCardRef, ApprovalCardStore
from .publication_k8s import (
    PublicationJobSettings,
    PublicationPayload,
    PublicationResourceNames,
    build_publication_resources,
    deterministic_publication_branch,
    publication_resource_names,
)
from .reply_sink import ReplySink, TargetRoute

_PR_MARKER = re.compile(r"^CURIE_PR_URL=(https://github\.com/[^\s]+/pull/\d+)$", re.MULTILINE)
logger = logging.getLogger(__name__)


class PublicationReconcileError(RuntimeError):
    """A durable publication could not reach a safe terminal state."""


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
    exists: bool = True


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

    def persist_result(
        self,
        publication_id: uuid.UUID,
        *,
        outcome: str,
        pr_url: str | None,
        error: str | None,
    ) -> None | Awaitable[None]: ...

    def pending_result(self, publication_id: uuid.UUID | None = None) -> Any: ...

    def mark_result_delivered(
        self, publication_id: uuid.UUID
    ) -> None | Awaitable[None]: ...

    def retry_result_delivery(
        self, publication_id: uuid.UUID, *, error: str
    ) -> None | Awaitable[None]: ...

    def retry(
        self, publication_id: uuid.UUID, *, error: str
    ) -> None | Awaitable[None]: ...


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
    def recover_pr_by_head(
        self,
        repo_full_name: str,
        branch: str,
        title: str,
        body: str,
        authorization_header: str,
    ) -> str | None | Awaitable[str | None]: ...


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


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
        card_store: ApprovalCardStore | None = None,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._cluster = cluster
        self._github = github
        self._replies = replies
        self._job_settings = job_settings
        self._card_store = card_store

    async def _report(self, target: ReplyTarget, route: TargetRoute, text: str) -> None:
        await self._replies.emit(
            ReplyUpdate(
                version=REPLY_WIRE_VERSION,
                event="reply.update",
                target=target,
                text=text,
            ),
            route=route,
            best_effort_unreachable=False,
        )

    async def _persist_result(
        self,
        work: PublicationWork,
        *,
        outcome: str,
        pr_url: str | None = None,
        error: str | None = None,
    ) -> None:
        await _resolve(
            self._store.persist_result(
                work.publication_id,
                outcome=outcome,
                pr_url=pr_url,
                error=error,
            )
        )

    async def _cleanup_credentials(self, names: PublicationResourceNames) -> None:
        cleanup = getattr(self._cluster, "cleanup_credentials", None)
        if cleanup is not None:
            await _resolve(cleanup(names))
            return
        await _resolve(self._cluster.cleanup(names))

    async def _cleanup_terminal(self, names: PublicationResourceNames) -> None:
        cleanup = getattr(self._cluster, "cleanup_terminal", None)
        if cleanup is not None:
            await _resolve(cleanup(names))

    async def _settle_card(self, result: Any, ref: ApprovalCardRef) -> None:
        decision: Literal["approved", "rejected"] | None
        if result.outcome in {"published", "failed"}:
            decision = "approved"
        elif result.outcome == "denied":
            decision = "rejected"
        else:
            decision = None
        await self._replies.emit(
            ReplyUpdate(
                version=REPLY_WIRE_VERSION,
                event="reply.update",
                target=ReplyTarget(
                    kind=ref.kind or result.target.kind,
                    address=ref.channel,
                    conversation_id=result.target.conversation_id,
                    reply_ref=ref.ts,
                ),
                message=OutboundMessage(version=MESSAGE_VERSION, text=ref.summary),
                settled=SettledOutcome(
                    requested_by=ref.requested_by,
                    decision=decision,
                ),
            ),
            route=TargetRoute(
                endpoint=ref.endpoint,
                adapter=ref.adapter if ref.kind else result.route.adapter,
            ),
            best_effort_unreachable=False,
        )

    async def deliver_pending_result(
        self,
        publication_id: uuid.UUID | None = None,
        *,
        credentials_already_clean: bool = False,
    ) -> bool:
        result = await _resolve(self._store.pending_result(publication_id))
        if result is None:
            return False
        names = publication_resource_names(result.publication_id)
        has_publication_resources = result.outcome in {"published", "failed"}
        if has_publication_resources and not credentials_already_clean:
            await self._cleanup_credentials(names)
        if result.outcome == "published":
            text = f"Published the approved changes: {result.pr_url}"
        elif result.outcome == "denied":
            text = (
                "Changes were not published: the publication request was denied, so no "
                "credential was redeemed and no branch was pushed."
            )
        elif result.outcome == "expired":
            text = (
                "Changes were not published: the publication approval expired before "
                "a decision was recorded."
            )
        else:
            text = f"Publication failed safely after approval: {result.error}"
        card_ref: ApprovalCardRef | None = None
        try:
            if self._card_store is not None:
                card_ref = await self._card_store.pop(result.target.conversation_id)
                if card_ref is not None and card_ref.approval_id != str(result.approval_id):
                    await self._card_store.restore(
                        result.target.conversation_id,
                        card_ref,
                    )
                    card_ref = None
            await self._report(result.target, result.route, text)
            if card_ref is not None:
                await self._settle_card(result, card_ref)
        except Exception as exc:
            try:
                if card_ref is not None and self._card_store is not None:
                    await self._card_store.restore(
                        result.target.conversation_id,
                        card_ref,
                    )
            finally:
                await _resolve(
                    self._store.retry_result_delivery(
                        result.publication_id,
                        error=(str(exc)[:2000] or type(exc).__name__),
                    )
                )
            raise
        await _resolve(self._store.mark_result_delivered(result.publication_id))
        if has_publication_resources:
            await self._cleanup_terminal(names)
        return True

    async def _terminalize(
        self,
        work: PublicationWork,
        *,
        outcome: str,
        pr_url: str | None = None,
        error: str | None = None,
        names: PublicationResourceNames,
    ) -> None:
        # The durable outcome is the source of truth. Credentials are removed
        # immediately after it commits; reply delivery is an independent outbox
        # attempt and may be retried without re-running GitHub mutation.
        await self._persist_result(work, outcome=outcome, pr_url=pr_url, error=error)
        has_publication_resources = outcome in {"published", "failed"}
        if has_publication_resources:
            await self._cleanup_credentials(names)
        await self.deliver_pending_result(
            work.publication_id,
            credentials_already_clean=has_publication_resources,
        )

    async def _bounded_setup_failure(
        self,
        work: PublicationWork,
        exc: Exception,
    ) -> None:
        error = str(exc)[:2000] or type(exc).__name__
        await _resolve(self._store.retry(work.publication_id, error=error))
        # retry() terminalizes at its durable cap. If it did, report the newly
        # available outbox record now; otherwise this is a no-op.
        await self.deliver_pending_result(work.publication_id)

    async def _recover_remote(
        self,
        work: PublicationWork,
        branch: str,
        credential: PublicationCredential,
    ) -> str | None:
        return await _resolve(
            self._github.recover_pr_by_head(
                work.repo_full_name,
                branch,
                work.title,
                work.body,
                credential.authorization_header,
            )
        )

    async def reconcile(self, work: PublicationWork) -> None:
        names = publication_resource_names(work.publication_id)
        if await _resolve(self._store.is_terminal(work.publication_id)):
            return

        if work.decision == "denied":
            await self._terminalize(work, outcome="denied", names=names)
            return

        # Pending, expired, and unknown states are never authority. Expiry is
        # terminalized by the API/store lane; it still creates no cluster or
        # GitHub side effect here.
        if work.decision != "approved":
            return

        branch = deterministic_publication_branch(work.publication_id)
        observation = await _resolve(self._cluster.observe(names.job))
        if observation.exists and observation.phase in {"pending", "running"}:
            return
        if observation.exists:
            pr_url = observation.pr_url or _marker_url(observation.logs)
            if pr_url is not None:
                await self._terminalize(
                    work,
                    outcome="published",
                    pr_url=pr_url,
                    names=names,
                )
                return

        credential: PublicationCredential
        recovered: str | None
        resources: Any | None = None
        try:
            credential = await _resolve(self._credentials.redeem(work.publication_id))
            recovered = await self._recover_remote(work, branch, credential)
            if recovered is None and observation.exists and observation.phase in {
                "failed",
                "succeeded",
            }:
                diagnostic = observation.error or (
                    "publication Job succeeded but no pull request was found for its branch"
                )
                raise PublicationReconcileError(diagnostic)

            if recovered is None:
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
        except Exception as exc:
            # Resource construction has not reached Kubernetes. Remote recovery
            # may have adopted/created the deterministic-head PR, but its own
            # query-before-POST/lost-response query makes the next attempt safe.
            # Release every setup failure through the durable bounded counter.
            await self._bounded_setup_failure(work, exc)
            return

        if recovered is not None:
            await self._terminalize(
                work,
                outcome="published",
                pr_url=recovered,
                names=names,
            )
            return
        if resources is None:
            raise PublicationReconcileError("publication resources were not constructed")

        try:
            # Server-side apply/create-or-adopt must use these deterministic
            # names. A crash immediately after the apiserver accepts this call
            # therefore re-adopts the same Job on the next lease.
            await _resolve(self._cluster.apply(resources))
            observation = await _resolve(self._cluster.observe(resources.names.job))
            if observation.phase in {"pending", "running"}:
                return
            pr_url = observation.pr_url or _marker_url(observation.logs)
            if pr_url is None:
                # Covers both crash-after-push and crash-after-REST-POST: the
                # authenticated recovery client adopts an existing PR or opens
                # one for the deterministic remote branch.
                pr_url = await self._recover_remote(work, branch, credential)
            if pr_url is None:
                raise PublicationReconcileError(
                    observation.error
                    or "publication Job finished but no pull request was found for its branch"
                )

            await self._terminalize(
                work,
                outcome="published",
                pr_url=pr_url,
                names=resources.names,
            )
        except Exception as exc:
            # A process-crash simulation deliberately raises immediately after
            # apply. At that point we cannot know whether the Job is live, so do
            # not mark the row failed or delete the idempotency anchor; let the
            # durable lease expire and re-adopt it.
            if not isinstance(exc, PublicationReconcileError):
                raise
            await self._bounded_setup_failure(work, exc)


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
            try:
                await self._reconciler.deliver_pending_result()
            except Exception:
                # The result lease was released (or dead-lettered) before the
                # error escaped. Publication mutation remains terminal and is
                # never repeated because a reply transport is unavailable.
                logger.exception("publication result delivery failed")
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
    "PublicationCredential",
    "PublicationJobObservation",
    "PublicationReconcileError",
    "PublicationReconciler",
    "PublicationReconcileLoop",
    "PublicationWork",
    "deterministic_publication_branch",
]
