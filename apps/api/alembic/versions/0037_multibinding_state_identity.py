"""Preserve legacy shared state and make its NULL identity singular (#1901).

Revision 0031 added ``binding_scope`` and defaulted every agent to
``memory=false``.  Rows written before that revision correctly retained NULL --
they were agent-wide -- but their owners then routed general-state requests to
a binding-specific scope and could no longer see those rows.  This repair turns
only unambiguous owners of legacy general state back to the shared posture.

PostgreSQL's ordinary unique constraints treat every NULL as distinct, so the
four-column state identity added by 0031 also allowed concurrent creators to
insert more than one shared row.  Recreating that same named constraint with
``NULLS NOT DISTINCT`` makes NULL the single shared binding identity while
leaving distinct non-NULL binding scopes isolated.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
AGENTS_TABLE = "agents"
STATE_TABLE = "workflow_state_entries"
STATE_CONSTRAINT = "uq_state_agent_scope_ns_key"

# Keep these literals pinned to ``routers.state.RESERVED_NAMESPACES`` and
# ``curie_runner.state.RESERVED_NAMESPACES``.  Memory and transcript are always
# agent-wide; their NULL rows are not evidence that general state was legacy
# shared state.
MEMORY_NAMESPACE = "memory"
TRANSCRIPT_NAMESPACE = "transcript"
_RESERVED_PARAMS = {
    "memory_namespace": MEMORY_NAMESPACE,
    "transcript_namespace": TRANSCRIPT_NAMESPACE,
}


def _lock_tables() -> None:
    """Freeze every inspected/written row in one deadlock-safe order."""

    conn = op.get_bind()
    # Agents must be first: deleting one cascades into workflow state, so taking
    # the locks in the opposite order could deadlock with that delete.  SHARE ROW
    # EXCLUSIVE blocks concurrent INSERT/UPDATE/DELETE while preserving reads,
    # and PostgreSQL holds both locks through Alembic's transaction boundary.
    conn.execute(sa.text(f"LOCK TABLE {SCHEMA}.{AGENTS_TABLE} IN SHARE ROW EXCLUSIVE MODE"))
    conn.execute(sa.text(f"LOCK TABLE {SCHEMA}.{STATE_TABLE} IN SHARE ROW EXCLUSIVE MODE"))


def _duplicate_null_identities() -> list[sa.Row[tuple[Any, ...]]]:
    """Return every identity that the stricter constraint cannot represent."""

    return list(
        op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT agent_id, namespace, key, count(*) AS row_count
                FROM {SCHEMA}.{STATE_TABLE}
                WHERE binding_scope IS NULL
                GROUP BY agent_id, namespace, key
                HAVING count(*) > 1
                ORDER BY agent_id, namespace, key
                """
            )
        )
        .all()
    )


def _mixed_scope_flip_candidates() -> list[sa.Row[tuple[Any, ...]]]:
    """Return false agents whose general-state posture is ambiguous."""

    return list(
        op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT agents.id AS agent_id,
                       count(*) FILTER (
                           WHERE state.binding_scope IS NULL
                       ) AS shared_rows,
                       count(*) FILTER (
                           WHERE state.binding_scope IS NOT NULL
                       ) AS isolated_rows
                FROM {SCHEMA}.{AGENTS_TABLE} AS agents
                JOIN {SCHEMA}.{STATE_TABLE} AS state
                  ON state.agent_id = agents.id
                WHERE agents.memory = false
                  AND state.namespace NOT IN (
                      :memory_namespace, :transcript_namespace
                  )
                GROUP BY agents.id
                HAVING bool_or(state.binding_scope IS NULL)
                   AND bool_or(state.binding_scope IS NOT NULL)
                ORDER BY agents.id
                """
            ),
            _RESERVED_PARAMS,
        )
        .all()
    )


def _refuse_ambiguous_state(
    duplicates: list[sa.Row[tuple[Any, ...]]],
    mixed_scope: list[sa.Row[tuple[Any, ...]]],
) -> None:
    if not duplicates and not mixed_scope:
        return

    findings: list[str] = []
    if duplicates:
        detail = "; ".join(
            f"{row.agent_id} {row.namespace}/{row.key} ({row.row_count} NULL rows)"
            for row in duplicates
        )
        findings.append(f"duplicate NULL identities: {detail}")
    if mixed_scope:
        detail = "; ".join(
            f"{row.agent_id} ({row.shared_rows} shared, {row.isolated_rows} isolated general rows)"
            for row in mixed_scope
        )
        findings.append(f"memory=false agents with mixed shared/isolated state: {detail}")

    raise RuntimeError(
        "cannot repair shared workflow-state identity (#1901); no agent, state "
        "row, or constraint was changed -- "
        + " | ".join(findings)
        + ". For each duplicate NULL identity, inspect its values and versions "
        "and explicitly merge or delete rows until one remains. For each "
        "memory=false mixed-scope agent, choose the intended shared or isolated "
        "policy and move/merge every general row into that one shape. Re-run "
        "this migration only after resolving every named finding."
    )


def upgrade() -> None:
    _lock_tables()

    # Complete both preflights before refusing so one failed attempt gives the
    # operator the whole deterministic remediation set.  They precede every
    # write and DDL operation; Alembic's transaction releases the locks and
    # leaves both data and revision stamp unchanged on refusal.
    duplicates = _duplicate_null_identities()
    mixed_scope = _mixed_scope_flip_candidates()
    _refuse_ambiguous_state(duplicates, mixed_scope)

    # A NULL row in memory/transcript is permanently shared and must not change
    # an agent's general-state policy.  Only a false owner of NULL non-reserved
    # state has the unambiguous pre-0031 shape this migration repairs.
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.{AGENTS_TABLE} AS agents
            SET memory = true
            WHERE agents.memory = false
              AND EXISTS (
                  SELECT 1
                  FROM {SCHEMA}.{STATE_TABLE} AS state
                  WHERE state.agent_id = agents.id
                    AND state.binding_scope IS NULL
                    AND state.namespace NOT IN (
                        :memory_namespace, :transcript_namespace
                    )
              )
            """
        ),
        _RESERVED_PARAMS,
    )

    op.drop_constraint(STATE_CONSTRAINT, STATE_TABLE, type_="unique", schema=SCHEMA)
    op.create_unique_constraint(
        STATE_CONSTRAINT,
        STATE_TABLE,
        ["agent_id", "binding_scope", "namespace", "key"],
        schema=SCHEMA,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint(STATE_CONSTRAINT, STATE_TABLE, type_="unique", schema=SCHEMA)
    op.create_unique_constraint(
        STATE_CONSTRAINT,
        STATE_TABLE,
        ["agent_id", "binding_scope", "namespace", "key"],
        schema=SCHEMA,
    )
    # Do not revert repaired memory flags.  This revision did not record their
    # provenance, so an operator-enabled shared agent is indistinguishable from
    # one repaired above; setting either false would knowingly hide shared rows.
