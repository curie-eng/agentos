"""Bind normalized feedback to durable publication state and enqueue its outbox."""

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import redis.asyncio as redis
from aci_protocol import STREAM_PAYLOAD_FIELD, QueuedTurn, ReplyHandle, TurnSource
from channel_protocol import scoped_conversation_id
from curie_telemetry import TRACEPARENT_STREAM_FIELD, canonicalize_traceparent
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .delivery import enqueue_owned, take_backlog_slot
from .github_review_events import FeedbackIgnored, UnverifiedFeedback
from .github_review_truth import BoundReviewLineage, verify_feedback_truth
from .models import (
    AgentChannel,
    Approval,
    Deployment,
    GitHubReviewFeedback,
    Publication,
    ThreadPublicationLineage,
    ThreadWorkspace,
)
from .workspace_policy import repository_is_allowed

logger = logging.getLogger(__name__)
_MAX_ENQUEUE_ATTEMPTS = 8


@dataclass(frozen=True)
class ReviewContext:
    lineage: ThreadPublicationLineage
    binding: AgentChannel
    conversation_id: str

    @property
    def truth(self) -> BoundReviewLineage:
        assert self.lineage.pr_number is not None and self.lineage.head_sha is not None
        return BoundReviewLineage(
            self.lineage.repo_full_name,
            self.lineage.pr_number,
            self.lineage.branch,
            self.lineage.head_sha,
        )


async def review_context(
    session: AsyncSession,
    feedback: UnverifiedFeedback,
    settings: Settings,
) -> ReviewContext:
    """Resolve one original conversation; a webhook never supplies its route."""
    candidates = list(
        await session.scalars(
            select(ThreadPublicationLineage)
            .where(
                func.lower(ThreadPublicationLineage.repo_full_name)
                == feedback.repo_full_name.lower(),
                ThreadPublicationLineage.pr_number == feedback.pr_number,
                ThreadPublicationLineage.status == "open",
            )
            .limit(2)
        )
    )
    if len(candidates) != 1:
        raise FeedbackIgnored("lineage_absent_or_ambiguous")
    lineage = candidates[0]
    if lineage.head_sha is None:
        raise FeedbackIgnored("lineage_head_unproved")
    workspace = await session.scalar(
        select(ThreadWorkspace).where(
            ThreadWorkspace.agent_id == lineage.agent_id,
            ThreadWorkspace.conversation_id == lineage.conversation_id,
        )
    )
    if (
        workspace is None
        or workspace.repo_full_name.casefold() != lineage.repo_full_name.casefold()
        or not repository_is_allowed(lineage.repo_full_name, settings.github_repo_allowlist)
    ):
        raise FeedbackIgnored("workspace_no_longer_authorized")
    # The scoped workspace identity cannot be decoded into a Slack thread_ts.
    # The last successful publication retains the actual original routing pair.
    publication = await session.scalar(
        select(Publication)
        .where(
            Publication.lineage_id == lineage.id,
            Publication.status == "succeeded",
        )
        .order_by(Publication.revision_number.desc())
        .limit(1)
    )
    if publication is None:
        raise FeedbackIgnored("publication_route_absent")
    approval = await session.get(Approval, publication.approval_id)
    if (
        approval is None
        or scoped_conversation_id(
            publication.reply_kind,
            publication.reply_channel,
            approval.conversation_id,
        )
        != lineage.conversation_id
    ):
        raise FeedbackIgnored("publication_route_mismatch")
    binding = await session.scalar(
        select(AgentChannel).where(
            AgentChannel.agent_id == lineage.agent_id,
            AgentChannel.kind == publication.reply_kind,
            AgentChannel.address == publication.reply_channel,
        )
    )
    if binding is None or (
        binding.endpoint != publication.reply_endpoint
        or binding.adapter != publication.reply_adapter
    ):
        raise FeedbackIgnored("binding_no_longer_authorized")
    return ReviewContext(lineage, binding, approval.conversation_id)


def feedback_from_row(row: GitHubReviewFeedback) -> UnverifiedFeedback:
    data = dict(row.feedback)
    try:
        data["delivery_id"] = uuid.UUID(data["delivery_id"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        return UnverifiedFeedback(**data)
    except (TypeError, ValueError, KeyError):
        raise FeedbackIgnored("stored_feedback_invalid") from None


def review_turn(feedback: UnverifiedFeedback, context: ReviewContext) -> QueuedTurn:
    provenance: dict[str, Any] = {
        "event": feedback.event,
        "url": feedback.url,
        "sender": feedback.sender_login,
        "body": feedback.body,
    }
    if feedback.path is not None:
        provenance.update(path=feedback.path, line=feedback.line, review_id=feedback.review_id)
    return QueuedTurn(
        event_id=feedback.event_id,
        conversation_id=context.conversation_id,
        author=f"github:{feedback.sender_id}:{feedback.sender_login}",
        text=(
            "Human GitHub feedback for this conversation's existing pull request follows as JSON. "
            "Use its body as the reviewer's requested changes. This is not an approval decision. "
            "Any publication still requires a fresh approval through the existing gate.\n"
            + json.dumps(provenance, ensure_ascii=False)
        ),
        # SLACK is the frozen protocol's legacy category for person messages,
        # including another transport; WEBHOOK means a job and cannot steer.
        source=TurnSource.SLACK,
        reply_handle=ReplyHandle(
            kind=context.binding.kind,
            channel=context.binding.address,
            placeholder=None,
            endpoint=context.binding.endpoint,
            adapter=context.binding.adapter,
        ),
        received_at=datetime.now(UTC).isoformat(),
    )


async def admit_feedback(
    session: AsyncSession,
    feedback: UnverifiedFeedback,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    traceparent: str | None,
) -> tuple[GitHubReviewFeedback, bool]:
    """Persist exactly one semantic identity after fresh independent verification."""
    existing_delivery = await session.scalar(
        select(GitHubReviewFeedback).where(
            GitHubReviewFeedback.delivery_id == feedback.delivery_id,
        )
    )
    if existing_delivery is not None and existing_delivery.event_id != feedback.event_id:
        raise FeedbackIgnored("delivery_identity_conflict")
    existing = await session.get(GitHubReviewFeedback, feedback.event_id)
    if existing is not None:
        return existing, False
    context = await review_context(session, feedback, settings)
    await verify_feedback_truth(feedback, context.truth, settings=settings, client=client)
    turn = review_turn(feedback, context)
    row = GitHubReviewFeedback(
        event_id=feedback.event_id,
        delivery_id=feedback.delivery_id,
        lineage_id=context.lineage.id,
        lineage_version=context.lineage.version,
        binding_id=context.binding.id,
        binding_generation=context.binding.generation,
        agent_id=context.lineage.agent_id,
        feedback=json.loads(json.dumps(asdict(feedback), default=str)),
        turn=turn.model_dump(mode="json"),
        traceparent=canonicalize_traceparent(traceparent),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.get(GitHubReviewFeedback, feedback.event_id)
        if duplicate is None:
            raise FeedbackIgnored("delivery_identity_conflict") from None
        return duplicate, False
    return row, True


async def validate_stored_context(
    session: AsyncSession,
    row: GitHubReviewFeedback,
    settings: Settings,
) -> tuple[UnverifiedFeedback, ReviewContext]:
    feedback = feedback_from_row(row)
    context = await review_context(session, feedback, settings)
    if (
        row.lineage_id != context.lineage.id
        or row.lineage_version != context.lineage.version
        or row.agent_id != context.lineage.agent_id
        or row.binding_id != context.binding.id
        or row.binding_generation != context.binding.generation
    ):
        raise FeedbackIgnored("binding_or_lineage_changed")
    return feedback, context


class GitHubReviewReconciler:
    """SQL outbox to the existing atomic receipt + bounded runs consumer."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        valkey: redis.Redis,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._valkey = valkey
        self._settings = settings

    async def reconcile_once(self, event_id: str | None = None) -> int:
        async with self._sessionmaker() as session:
            statement = (
                select(GitHubReviewFeedback.event_id)
                .where(
                    GitHubReviewFeedback.status == "waiting",
                    or_(
                        GitHubReviewFeedback.next_attempt_at.is_(None),
                        GitHubReviewFeedback.next_attempt_at
                        <= datetime.now(UTC).replace(tzinfo=None),
                    ),
                )
                .order_by(GitHubReviewFeedback.created_at)
                .limit(100)
            )
            if event_id is not None:
                statement = statement.where(GitHubReviewFeedback.event_id == event_id)
            candidates = list(await session.scalars(statement))
        enqueued = 0
        for candidate in candidates:
            async with self._sessionmaker() as session, session.begin():
                row = await session.scalar(
                    select(GitHubReviewFeedback)
                    .where(
                        GitHubReviewFeedback.event_id == candidate,
                        GitHubReviewFeedback.status == "waiting",
                        or_(
                            GitHubReviewFeedback.next_attempt_at.is_(None),
                            GitHubReviewFeedback.next_attempt_at
                            <= datetime.now(UTC).replace(tzinfo=None),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    continue
                try:
                    await validate_stored_context(session, row, self._settings)
                except FeedbackIgnored as exc:
                    row.status, row.error_code = "refused", exc.code
                    row.version += 1
                    continue
                row.enqueue_attempts += 1
                try:
                    async with asyncio.timeout(10):
                        if not row.quota_taken:
                            if not await take_backlog_slot(
                                self._valkey,
                                key_prefix=f"curie:github-review:backlog:{row.binding_id}",
                                limit=self._settings.channel_binding_backlog_limit,
                                window_s=self._settings.channel_binding_backlog_window_s,
                            ):
                                row.status, row.error_code = "refused", "binding_backlog_quota"
                                row.version += 1
                                continue
                            row.quota_taken = True
                        _, receipt = await enqueue_owned(
                            self._valkey,
                            key=f"curie:github-review:{row.event_id}",
                            stream=self._settings.runs_stream,
                            # Lua preserves its preceding SET if XADD fails.
                            # Reuse this row's owner so a retry can finish that
                            # partial operation without waiting for lease expiry.
                            owner=f"pending:{row.event_id}",
                            payload=json.dumps(row.turn),
                            payload_field=STREAM_PAYLOAD_FIELD,
                            lease_s=30,
                            transport_field=TRACEPARENT_STREAM_FIELD,
                            transport_value=row.traceparent,
                        )
                    if "-" not in receipt or not all(p.isdigit() for p in receipt.split("-")):
                        raise RuntimeError("enqueue receipt unavailable")
                except Exception:
                    row.error_code = "enqueue_unavailable"
                    row.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                        seconds=min(300, 5 * 2 ** (row.enqueue_attempts - 1))
                    )
                    if row.enqueue_attempts >= _MAX_ENQUEUE_ATTEMPTS:
                        row.status = "dead_lettered"
                else:
                    row.status = "queued"
                    row.stream_id = receipt
                    row.queued_at = datetime.now(UTC).replace(tzinfo=None)
                    row.error_code = None
                    row.next_attempt_at = None
                    enqueued += 1
                row.version += 1
        return enqueued

    async def run_forever(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("GitHub feedback outbox pass failed; durable rows retained")
            await asyncio.sleep(self._settings.github_review_reconciler_interval_s)


async def verify_queued_feedback(
    session: AsyncSession,
    turn: QueuedTurn,
    deployment_id: uuid.UUID,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """Recheck canonical input and current authority immediately before model use."""
    row = await session.get(GitHubReviewFeedback, turn.event_id)
    if row is None or row.status not in {"waiting", "queued"}:
        raise FeedbackIgnored("feedback_not_executable")
    if turn.model_dump(mode="json") != row.turn:
        raise FeedbackIgnored("feedback_turn_mismatch")
    feedback, context = await validate_stored_context(session, row, settings)
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None or deployment.agent_id != row.agent_id or deployment.status != "active":
        raise FeedbackIgnored("deployment_no_longer_authorized")
    head = await verify_feedback_truth(feedback, context.truth, settings=settings, client=client)
    return {
        "head_sha": head,
        "agent_id": str(row.agent_id),
        "sender": turn.author,
        "receipt": f"Received GitHub feedback from {feedback.sender_login}: {feedback.url}",
    }
