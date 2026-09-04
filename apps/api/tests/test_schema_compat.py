"""Database/application compatibility window (#2300).

The planner, serve check, expand rollback, irreversible refusal, redacted
output, and crash/retry resume are the behavior this module pins. Production
code lives in ``curie_api.schema_compat``; this file never migrates by calling
``alembic upgrade head`` from an API pod startup path.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from curie_api.config import get_settings
from curie_api.schema_compat import (
    KIND_EXPAND,
    KIND_IRREVERSIBLE,
    AppWindow,
    apply_upgrade,
    assert_servable,
    can_serve,
    current_revision,
    load_kinds,
    load_window,
    plan_upgrade,
    render_decision,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"
HEAD = "0040"
PREV = "0039"


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return cfg


def _sql(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def run() -> list[Any]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params or {})
                return list(result.fetchall())
        finally:
            await engine.dispose()

    import asyncio

    return asyncio.run(run())


def _exec(sql: str, params: dict[str, Any] | None = None) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params or {})
        finally:
            await engine.dispose()

    import asyncio

    asyncio.run(run())


def test_released_application_declares_a_machine_readable_window() -> None:
    window = load_window()
    assert window.schema_min == HEAD
    assert window.schema_head == HEAD
    kinds = load_kinds()
    assert kinds[HEAD] == KIND_EXPAND
    assert kinds[PREV] == KIND_EXPAND
    assert kinds["0016"] == KIND_IRREVERSIBLE


def test_planner_applies_an_expand_and_marks_rollback_compatible() -> None:
    window = AppWindow(schema_min=HEAD, schema_head=HEAD)
    kinds = {PREV: KIND_EXPAND, HEAD: KIND_EXPAND}
    decision = plan_upgrade(
        current_revision=PREV,
        window=window,
        kinds=kinds,
        pending=(HEAD,),
        forward_only=False,
    )
    assert decision.action == "apply"
    assert decision.rollback_compatible is True
    assert decision.pending[0].revision == HEAD
    assert decision.pending[0].kind == KIND_EXPAND


def test_planner_refuses_irreversible_before_mutation() -> None:
    window = AppWindow(schema_min="0017", schema_head="0017")
    kinds = {"0016": KIND_IRREVERSIBLE, "0017": KIND_EXPAND}
    decision = plan_upgrade(
        current_revision="0015",
        window=window,
        kinds=kinds,
        pending=("0016", "0017"),
        forward_only=False,
    )
    assert decision.action == "refuse"
    assert decision.rollback_compatible is False
    assert "0016" in decision.reason
    assert "forward-only" in decision.reason.lower()


def test_planner_applies_irreversible_only_with_forward_only() -> None:
    window = AppWindow(schema_min="0017", schema_head="0017")
    kinds = {"0016": KIND_IRREVERSIBLE, "0017": KIND_EXPAND}
    decision = plan_upgrade(
        current_revision="0015",
        window=window,
        kinds=kinds,
        pending=("0016", "0017"),
        forward_only=True,
    )
    assert decision.action == "apply"
    assert decision.rollback_compatible is False
    assert decision.forward_only is True


def test_empty_database_install_does_not_refuse_historical_irreversible() -> None:
    window = AppWindow(schema_min=HEAD, schema_head=HEAD)
    kinds = load_kinds()
    decision = plan_upgrade(
        current_revision=None,
        window=window,
        kinds=kinds,
        pending=(HEAD,),
        forward_only=False,
    )
    assert decision.action == "apply"
    assert decision.rollback_compatible is False


def test_already_at_head_is_noop() -> None:
    window = AppWindow(schema_min=HEAD, schema_head=HEAD)
    decision = plan_upgrade(
        current_revision=HEAD,
        window=window,
        kinds={HEAD: KIND_EXPAND},
        pending=(),
        forward_only=False,
    )
    assert decision.action == "noop"


def test_assert_servable_refuses_below_min(isolated_migration_db: None) -> None:
    import asyncio

    cfg = _alembic_config()
    command.upgrade(cfg, PREV)
    with pytest.raises(RuntimeError, match="below application min"):
        asyncio.run(assert_servable())
    command.upgrade(cfg, HEAD)
    asyncio.run(assert_servable())


def test_n_minus_one_can_serve_an_unknown_newer_expand() -> None:
    window = AppWindow(schema_min=PREV, schema_head=PREV)
    known = {PREV, "0038"}
    assert can_serve(HEAD, window, known) is True
    assert can_serve(PREV, window, known) is True
    assert can_serve("0038", window, known) is False
    assert can_serve(None, window, known) is False


def test_decision_json_is_redacted() -> None:
    window = AppWindow(schema_min=HEAD, schema_head=HEAD)
    decision = plan_upgrade(
        current_revision=PREV,
        window=window,
        kinds={HEAD: KIND_EXPAND},
        pending=(HEAD,),
        forward_only=False,
    )
    payload = json.dumps(render_decision(decision))
    lowered = payload.lower()
    assert "postgresql" not in lowered
    assert "password" not in lowered
    assert "database_url" not in lowered
    assert PREV in payload
    assert HEAD in payload


def test_upgrade_then_n_minus_one_serve_same_nonempty_database(
    isolated_migration_db: None,
) -> None:
    """Upgrade 0039 -> 0040 on one seeded database, then serve as N-1.

    0040 is an expand (nullable column). The N-1 application does not know
    revision 0040; after the upgrade it must still read the seeded row.
    """
    cfg = _alembic_config()
    command.upgrade(cfg, PREV)
    assert current_revision() == PREV

    approval_id = uuid.uuid4()
    _exec(
        "INSERT INTO curie.approvals (id, conversation_id, author, summary, "
        "reply_kind, reply_channel, reply_placeholder, dedupe_key, status) "
        "VALUES (:id, :conversation_id, :author, :summary, :reply_kind, "
        ":reply_channel, :reply_placeholder, :dedupe_key, 'pending')",
        {
            "id": approval_id,
            "conversation_id": "th-compat-2300",
            "author": "U1",
            "summary": "seeded before expand",
            "reply_kind": "slack",
            "reply_channel": "C0EXAMPLE1",
            "reply_placeholder": None,
            "dedupe_key": uuid.uuid4().hex,
        },
    )

    outcome = apply_upgrade(forward_only=False, alembic_config=cfg)
    assert outcome.action == "apply"
    assert outcome.outcome == "applied"
    assert outcome.rollback_compatible is True
    assert current_revision() == HEAD

    rows = _sql(
        "SELECT summary FROM curie.approvals WHERE id = :id",
        {"id": approval_id},
    )
    assert rows == [("seeded before expand",)]

    # The expand column exists and is null on the preserved row.
    col = _sql(
        "SELECT workspace_conversation_id FROM curie.publications LIMIT 0"
    )
    assert col == []
    pubs = _sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'publications' "
        "AND column_name = 'workspace_conversation_id'"
    )
    assert pubs, "0040 expand column must exist after upgrade"

    n1 = AppWindow(schema_min=PREV, schema_head=PREV)
    assert can_serve(current_revision(), n1, {PREV}) is True

    # Red-on-revert: the N application (min=0040) refuses a database that
    # is still at 0039. Expand rollback is application rollback, not schema
    # downgrade; serving N against the pre-expand revision is the unsupported
    # direction.
    n = load_window()
    assert can_serve(PREV, n, {PREV, HEAD}) is False


def test_crash_retry_does_not_double_apply(
    isolated_migration_db: None, tmp_path: Path
) -> None:
    """Two pending expands: first lands, second raises, retry resumes.

    Alembic's version table is the durable phase boundary. The first
    revision must not run again; the unique insert in the second must
    land once.
    """
    cfg = _alembic_config()
    command.upgrade(cfg, HEAD)
    _exec(
        "CREATE TABLE curie.compat_probe ("
        "rev text primary key, "
        "applied_at timestamptz not null default now())"
    )

    alembic_copy = tmp_path / "alembic"
    shutil.copytree(ALEMBIC_DIR, alembic_copy)
    versions = alembic_copy / "versions"
    (versions / "0041_compat_first.py").write_text(
        '''
revision = "0041"
down_revision = "0040"

def upgrade():
    from alembic import op
    op.execute(
        "INSERT INTO curie.compat_probe (rev) VALUES ('0041')"
    )

def downgrade():
    from alembic import op
    op.execute("DELETE FROM curie.compat_probe WHERE rev = '0041'")
'''
    )
    (versions / "0042_compat_second.py").write_text(
        '''
import os
revision = "0042"
down_revision = "0041"

def upgrade():
    from alembic import op
    if os.environ.get("CURIE_COMPAT_PROBE_CRASH") == "1":
        raise RuntimeError("injected crash after 0041")
    op.execute(
        "INSERT INTO curie.compat_probe (rev) VALUES ('0042')"
    )

def downgrade():
    from alembic import op
    op.execute("DELETE FROM curie.compat_probe WHERE rev = '0042'")
'''
    )
    probe_cfg = Config()
    probe_cfg.set_main_option("script_location", str(alembic_copy))

    os.environ["CURIE_COMPAT_PROBE_CRASH"] = "1"
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            apply_upgrade(
                forward_only=False,
                alembic_config=probe_cfg,
                window=AppWindow(schema_min=HEAD, schema_head="0042"),
                kinds={
                    **load_kinds(),
                    "0041": KIND_EXPAND,
                    "0042": KIND_EXPAND,
                },
            )
    finally:
        os.environ.pop("CURIE_COMPAT_PROBE_CRASH", None)

    assert current_revision() == "0041"
    rows = _sql("SELECT rev FROM curie.compat_probe ORDER BY rev")
    assert [r[0] for r in rows] == ["0041"]

    outcome = apply_upgrade(
        forward_only=False,
        alembic_config=probe_cfg,
        window=AppWindow(schema_min=HEAD, schema_head="0042"),
        kinds={
            **load_kinds(),
            "0041": KIND_EXPAND,
            "0042": KIND_EXPAND,
        },
    )
    assert outcome.outcome == "applied"
    assert current_revision() == "0042"
    rows = _sql("SELECT rev FROM curie.compat_probe ORDER BY rev")
    assert [r[0] for r in rows] == ["0041", "0042"]

    again = apply_upgrade(
        forward_only=False,
        alembic_config=probe_cfg,
        window=AppWindow(schema_min=HEAD, schema_head="0042"),
        kinds={
            **load_kinds(),
            "0041": KIND_EXPAND,
            "0042": KIND_EXPAND,
        },
    )
    assert again.action == "noop"
    rows = _sql("SELECT rev FROM curie.compat_probe ORDER BY rev")
    assert [r[0] for r in rows] == ["0041", "0042"]
