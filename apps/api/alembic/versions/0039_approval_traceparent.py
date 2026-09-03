"""Persist private approval trace continuity (#2204).

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "approvals"


def upgrade() -> None:
    # Existing and rolling-deploy rows have no originating HTTP carrier. NULL
    # keeps that provenance honest and makes recovery start a safe root.
    op.add_column(
        TABLE,
        sa.Column("traceparent", sa.String(length=55), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(TABLE, "traceparent", schema=SCHEMA)
