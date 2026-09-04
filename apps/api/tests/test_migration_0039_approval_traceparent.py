"""Migration 0039: private, bounded approval trace continuity."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from curie_api.config import get_settings
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


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


def _seed_legacy_approval(approval_id: uuid.UUID) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO curie.approvals "
                        "(id, conversation_id, author, summary, reply_kind, "
                        "reply_channel, dedupe_key, status) VALUES "
                        "(:id, 'thread-legacy', 'U0REQUEST1', 'Approve action', "
                        "'slack', 'C0EXAMPLE1', :dedupe, 'pending')"
                    ),
                    {"id": approval_id, "dedupe": f"legacy-{approval_id.hex}"},
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _write_traceparent(approval_id: uuid.UUID, value: str) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE curie.approvals SET traceparent = :value "
                        "WHERE id = :id"
                    ),
                    {"id": approval_id, "value": value},
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_0039_adds_nullable_bounded_private_carrier_and_round_trips(
    isolated_migration_db: None,
) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "0038")
    approval_id = uuid.uuid4()
    _seed_legacy_approval(approval_id)

    command.upgrade(cfg, "head")

    assert _rows(
        "SELECT is_nullable, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'approvals' "
        "AND column_name = 'traceparent'"
    ) == [{"is_nullable": "YES", "character_maximum_length": 55}]
    assert _rows(
        "SELECT traceparent FROM curie.approvals WHERE id = :id",
        {"id": approval_id},
    ) == [{"traceparent": None}]
    valid = "00-2123456789abcdef0123456789abcdef-2123456789abcdef-01"
    assert len(valid) == 55
    _write_traceparent(approval_id, valid)
    assert _rows(
        "SELECT traceparent FROM curie.approvals WHERE id = :id",
        {"id": approval_id},
    ) == [{"traceparent": valid}]
    with pytest.raises(DBAPIError):
        _write_traceparent(approval_id, "x" * 56)

    command.downgrade(cfg, "0038")
    assert _rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'approvals' "
        "AND column_name = 'traceparent'"
    ) == []

    command.upgrade(cfg, "head")
    assert _rows(
        "SELECT is_nullable, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'approvals' "
        "AND column_name = 'traceparent'"
    ) == [{"is_nullable": "YES", "character_maximum_length": 55}]
