"""Authenticate approval actors and bind console sessions to subjects (#1531).

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
AUDIT_TABLE = "approval_audit_entries"
CONSOLE_TABLE = "console_sessions"
PRINCIPAL_KIND_CONSTRAINT = "approval_audit_principal_kind_ck"


def upgrade() -> None:
    # Nullable kind plus false default preserves the honest meaning of every
    # existing asserted/system row; no historical record is retro-labelled.
    op.add_column(
        AUDIT_TABLE,
        sa.Column("principal_kind", sa.String(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        AUDIT_TABLE,
        sa.Column(
            "authenticated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        PRINCIPAL_KIND_CONSTRAINT,
        AUDIT_TABLE,
        "principal_kind IS NULL OR principal_kind IN ('chat', 'console', 'operator')",
        schema=SCHEMA,
    )
    op.add_column(
        CONSOLE_TABLE,
        sa.Column("subject", sa.String(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(CONSOLE_TABLE, "subject", schema=SCHEMA)
    op.drop_constraint(
        PRINCIPAL_KIND_CONSTRAINT,
        AUDIT_TABLE,
        type_="check",
        schema=SCHEMA,
    )
    op.drop_column(AUDIT_TABLE, "authenticated", schema=SCHEMA)
    op.drop_column(AUDIT_TABLE, "principal_kind", schema=SCHEMA)
