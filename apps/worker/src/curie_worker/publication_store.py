"""Durable SKIP-LOCKED/CAS work queue for repository publications."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from channel_protocol.reply import ReplyTarget
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .publication_loop import PublicationWork
from .reply_sink import TargetRoute

_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PublicationStoreError(RuntimeError):
    """A durable publication transition lost its compare-and-swap."""


@dataclass(frozen=True)
class PublicationResult:
    """One leased terminal result awaiting delivery to its requesting thread."""

    publication_id: uuid.UUID
    approval_id: uuid.UUID
    outcome: str
    pr_url: str | None
    error: str | None
    target: ReplyTarget
    route: TargetRoute
    attempt: int
    version: int


class PostgresPublicationStore:
    """Claims one approval decision at a time without cross-worker blocking."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str,
        lease_owner: str,
        lease_seconds: int = 60,
        result_max_attempts: int = 5,
        reconcile_max_attempts: int = 10,
    ) -> None:
        if not _SAFE_SCHEMA.fullmatch(schema):
            raise ValueError("publication database schema is invalid")
        if not lease_owner:
            raise ValueError("publication lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("publication lease seconds must be positive")
        if result_max_attempts <= 0 or reconcile_max_attempts <= 0:
            raise ValueError("publication attempt limits must be positive")
        self._engine = engine
        self._table = f'"{schema}".publications'
        self._approvals = f'"{schema}".approvals'
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._result_max_attempts = result_max_attempts
        self._reconcile_max_attempts = reconcile_max_attempts
        self._versions: dict[uuid.UUID, int] = {}

    async def claim_next(self) -> PublicationWork | None:
        statement = text(
            f"""
            SELECT p.id, p.approval_id, p.repo_full_name, p.status, p.version,
                   p.base_sha, p.patch_bytes, p.changed_paths, p.title, p.body,
                   p.reply_kind, p.reply_channel, p.reply_placeholder,
                   p.reply_endpoint, p.reply_adapter,
                   a.conversation_id
              FROM {self._table} p
              JOIN {self._approvals} a ON a.id = p.approval_id
             WHERE p.patch_bytes IS NOT NULL
               AND p.status IN ('approved', 'launching', 'running')
               AND p.reconcile_attempts < :max_attempts
               AND p.reconcile_dead_lettered_at IS NULL
               AND (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
             ORDER BY p.created_at, p.id
             FOR UPDATE OF p SKIP LOCKED
             LIMIT 1
            """
        )
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    statement, {"max_attempts": self._reconcile_max_attempts}
                )
            ).mappings().first()
            if row is None:
                return None
            publication_id = uuid.UUID(str(row["id"]))
            previous_version = int(row["version"])
            status = str(row["status"])
            next_status = "launching" if status == "approved" else status
            updated = (
                await connection.execute(
                    text(
                        f"""
                        UPDATE {self._table}
                           SET status = :status,
                               lease_owner = :owner,
                               lease_expires_at = now() + :lease,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :id AND version = :version
                     RETURNING version
                        """
                    ),
                    {
                        "status": next_status,
                        "owner": self._lease_owner,
                        "lease": timedelta(seconds=self._lease_seconds),
                        "id": publication_id,
                        "version": previous_version,
                    },
                )
            ).scalar_one_or_none()
            if updated is None:
                raise PublicationStoreError("publication claim CAS was lost")
            version = int(updated)
            self._versions[publication_id] = version

        patch = row["patch_bytes"]
        if not isinstance(patch, bytes):
            patch = bytes(patch)
        paths = row["changed_paths"] or []
        return PublicationWork(
            publication_id=publication_id,
            approval_id=uuid.UUID(str(row["approval_id"])),
            decision="approved",
            repo_full_name=str(row["repo_full_name"]),
            base_sha=str(row["base_sha"]),
            patch=patch,
            changed_paths=tuple(str(path) for path in paths),
            title=str(row["title"]),
            body=str(row["body"]),
            target=ReplyTarget(
                kind=str(row["reply_kind"]),
                address=str(row["reply_channel"]),
                conversation_id=str(row["conversation_id"]),
                reply_ref=row["reply_placeholder"],
            ),
            route=TargetRoute(
                endpoint=row["reply_endpoint"],
                adapter=row["reply_adapter"],
            ),
            version=version,
        )

    async def is_terminal(self, publication_id: uuid.UUID) -> bool:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        f"""
                        SELECT status, patch_bytes IS NULL AS patch_cleared
                          FROM {self._table}
                         WHERE id = :id
                        """
                    ),
                    {"id": publication_id},
                )
            ).mappings().first()
        if row is None:
            return True
        status = str(row["status"])
        return status in {"succeeded", "failed", "expired"} or (
            status == "denied" and bool(row["patch_cleared"])
        )

    async def complete(
        self, publication_id: uuid.UUID, *, outcome: str, pr_url: str | None
    ) -> None:
        await self.persist_result(
            publication_id, outcome=outcome, pr_url=pr_url, error=None
        )

    async def fail(self, publication_id: uuid.UUID, *, error: str) -> None:
        await self.persist_result(
            publication_id, outcome="failed", pr_url=None, error=error
        )

    async def persist_result(
        self,
        publication_id: uuid.UUID,
        *,
        outcome: str,
        pr_url: str | None,
        error: str | None,
    ) -> None:
        """Persist the outcome and clear private work before any reply attempt."""

        status = {
            "published": "succeeded",
            "succeeded": "succeeded",
            "denied": "denied",
            "expired": "expired",
            "failed": "failed",
        }.get(outcome)
        if status is None:
            raise ValueError(f"unsupported publication outcome {outcome!r}")
        await self._terminal_cas(
            publication_id,
            status=status,
            result_url=pr_url,
            error=error[:2000] if error else None,
        )

    async def pending_result(
        self, publication_id: uuid.UUID | None = None
    ) -> PublicationResult | None:
        """Lease one undelivered terminal result from the durable outbox."""

        id_filter = "AND p.id = :requested_id" if publication_id is not None else ""
        statement = text(
            f"""
            SELECT p.id, p.approval_id, p.status, p.result_url, p.error, p.version,
                   p.result_delivery_attempts, p.reply_kind, p.reply_channel,
                   p.reply_placeholder, p.reply_endpoint, p.reply_adapter,
                   a.conversation_id
              FROM {self._table} p
              JOIN {self._approvals} a ON a.id = p.approval_id
             WHERE p.status IN ('denied', 'expired', 'succeeded', 'failed')
               AND p.patch_bytes IS NULL
               AND p.result_reported_at IS NULL
               AND p.result_delivery_dead_lettered_at IS NULL
               AND p.result_delivery_attempts < :max_attempts
               AND (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
               {id_filter}
             ORDER BY p.terminal_at, p.id
             FOR UPDATE OF p SKIP LOCKED
             LIMIT 1
            """
        )
        params: dict[str, object] = {"max_attempts": self._result_max_attempts}
        if publication_id is not None:
            params["requested_id"] = publication_id
        async with self._engine.begin() as connection:
            row = (await connection.execute(statement, params)).mappings().first()
            if row is None:
                return None
            result_id = uuid.UUID(str(row["id"]))
            updated = (
                await connection.execute(
                    text(
                        f"""
                        UPDATE {self._table}
                           SET result_delivery_attempts = result_delivery_attempts + 1,
                               lease_owner = :owner,
                               lease_expires_at = now() + :lease,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :id AND version = :version
                     RETURNING version, result_delivery_attempts
                        """
                    ),
                    {
                        "owner": self._lease_owner,
                        "lease": timedelta(seconds=self._lease_seconds),
                        "id": result_id,
                        "version": int(row["version"]),
                    },
                )
            ).mappings().first()
            if updated is None:
                raise PublicationStoreError("publication result claim CAS was lost")
            version = int(updated["version"])
            self._versions[result_id] = version
        status = str(row["status"])
        return PublicationResult(
            publication_id=result_id,
            approval_id=uuid.UUID(str(row["approval_id"])),
            outcome="published" if status == "succeeded" else status,
            pr_url=row["result_url"],
            error=row["error"],
            target=ReplyTarget(
                kind=str(row["reply_kind"]),
                address=str(row["reply_channel"]),
                conversation_id=str(row["conversation_id"]),
                reply_ref=row["reply_placeholder"],
            ),
            route=TargetRoute(
                endpoint=row["reply_endpoint"], adapter=row["reply_adapter"]
            ),
            attempt=int(updated["result_delivery_attempts"]),
            version=version,
        )

    async def mark_result_delivered(self, publication_id: uuid.UUID) -> None:
        """Acknowledge an outbox result only after the adapter accepted it."""

        await self._result_delivery_cas(publication_id, delivered=True, error=None)

    async def retry_result_delivery(
        self, publication_id: uuid.UUID, *, error: str
    ) -> None:
        """Release a failed reply lease, dead-lettering at the delivery cap."""

        await self._result_delivery_cas(
            publication_id, delivered=False, error=error[:2000]
        )

    async def _result_delivery_cas(
        self, publication_id: uuid.UUID, *, delivered: bool, error: str | None
    ) -> None:
        version = self._versions.get(publication_id)
        if version is None:
            raise PublicationStoreError("publication result has no owned lease version")
        async with self._engine.begin() as connection:
            updated = (
                await connection.execute(
                    text(
                        f"""
                        UPDATE {self._table}
                           SET result_reported_at = CASE WHEN :delivered THEN now()
                                                        ELSE result_reported_at END,
                               result_delivery_error = :error,
                               result_delivery_dead_lettered_at = CASE
                                   WHEN NOT :delivered
                                    AND result_delivery_attempts >= :max_attempts
                                   THEN now()
                                   ELSE result_delivery_dead_lettered_at
                               END,
                               lease_owner = NULL,
                               lease_expires_at = NULL,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :id AND version = :version AND lease_owner = :owner
                     RETURNING version
                        """
                    ),
                    {
                        "delivered": delivered,
                        "error": error,
                        "max_attempts": self._result_max_attempts,
                        "id": publication_id,
                        "version": version,
                        "owner": self._lease_owner,
                    },
                )
            ).scalar_one_or_none()
        if updated is None:
            raise PublicationStoreError("publication result delivery CAS was lost")
        self._versions[publication_id] = int(updated)

    async def retry(self, publication_id: uuid.UUID, *, error: str) -> None:
        """Release reconcile work, or dead-letter it into a reportable failure."""

        version = self._versions.get(publication_id)
        if version is None:
            raise PublicationStoreError("publication has no owned lease version")
        async with self._engine.begin() as connection:
            updated = (
                await connection.execute(
                    text(
                        f"""
                        UPDATE {self._table}
                           SET reconcile_attempts = reconcile_attempts + 1,
                               status = CASE WHEN reconcile_attempts + 1 >= :max_attempts
                                             THEN 'failed' ELSE status END,
                               error = :error,
                               patch_bytes = CASE WHEN reconcile_attempts + 1 >= :max_attempts
                                                  THEN NULL ELSE patch_bytes END,
                               terminal_at = CASE WHEN reconcile_attempts + 1 >= :max_attempts
                                                  THEN COALESCE(terminal_at, now())
                                                  ELSE terminal_at END,
                               reconcile_dead_lettered_at = CASE
                                   WHEN reconcile_attempts + 1 >= :max_attempts THEN now()
                                   ELSE reconcile_dead_lettered_at END,
                               lease_owner = NULL,
                               lease_expires_at = NULL,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :id AND version = :version AND lease_owner = :owner
                     RETURNING version
                        """
                    ),
                    {
                        "max_attempts": self._reconcile_max_attempts,
                        "error": error[:2000],
                        "id": publication_id,
                        "version": version,
                        "owner": self._lease_owner,
                    },
                )
            ).scalar_one_or_none()
        if updated is None:
            raise PublicationStoreError("publication retry CAS was lost")
        self._versions[publication_id] = int(updated)

    async def _terminal_cas(
        self,
        publication_id: uuid.UUID,
        *,
        status: str,
        result_url: str | None,
        error: str | None,
    ) -> None:
        version = self._versions.get(publication_id)
        if version is None:
            raise PublicationStoreError("publication has no owned lease version")
        async with self._engine.begin() as connection:
            updated = (
                await connection.execute(
                    text(
                        f"""
                        UPDATE {self._table}
                           SET status = :status,
                               result_url = :result_url,
                               error = :error,
                               patch_bytes = NULL,
                               lease_owner = NULL,
                               lease_expires_at = NULL,
                               terminal_at = COALESCE(terminal_at, now()),
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :id
                           AND version = :version
                           AND lease_owner = :owner
                     RETURNING version
                        """
                    ),
                    {
                        "status": status,
                        "result_url": result_url,
                        "error": error,
                        "id": publication_id,
                        "version": version,
                        "owner": self._lease_owner,
                    },
                )
            ).scalar_one_or_none()
        if updated is None:
            if await self.is_terminal(publication_id):
                return
            raise PublicationStoreError("publication terminal CAS was lost")
        self._versions[publication_id] = int(updated)
