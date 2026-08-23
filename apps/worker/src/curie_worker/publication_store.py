"""Durable SKIP-LOCKED/CAS work queue for repository publications."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from channel_protocol.reply import ReplyTarget
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .publication_loop import PublicationWork
from .reply_sink import TargetRoute

_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PublicationStoreError(RuntimeError):
    """A durable publication transition lost its compare-and-swap."""


class PostgresPublicationStore:
    """Claims one approval decision at a time without cross-worker blocking."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str,
        lease_owner: str,
        lease_seconds: int = 60,
    ) -> None:
        if not _SAFE_SCHEMA.fullmatch(schema):
            raise ValueError("publication database schema is invalid")
        if not lease_owner:
            raise ValueError("publication lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("publication lease seconds must be positive")
        self._engine = engine
        self._table = f'"{schema}".publications'
        self._approvals = f'"{schema}".approvals'
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
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
               AND p.status IN ('approved', 'denied', 'launching', 'running')
               AND (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
             ORDER BY p.created_at, p.id
             FOR UPDATE OF p SKIP LOCKED
             LIMIT 1
            """
        )
        async with self._engine.begin() as connection:
            row = (await connection.execute(statement)).mappings().first()
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
            decision="denied" if status == "denied" else "approved",
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
        status = {"published": "succeeded", "denied": "denied"}.get(outcome)
        if status is None:
            raise ValueError(f"unsupported publication outcome {outcome!r}")
        await self._terminal_cas(
            publication_id,
            status=status,
            result_url=pr_url,
            error=None,
        )

    async def fail(self, publication_id: uuid.UUID, *, error: str) -> None:
        await self._terminal_cas(
            publication_id,
            status="failed",
            result_url=None,
            error=error[:2000],
        )

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

