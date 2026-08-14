"""allow approvals without a reply placeholder (#1528)

The worker may enqueue a final only reply target without a preposted message to
edit. The explicit channel and endpoint still select where the reply goes, so
the absent placeholder is durable approval state rather than a missing value.

Existing placeholder strings keep their edit target unchanged. This revision
only relaxes the column constraint and does not rewrite stored values.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "approvals"
COLUMN = "reply_placeholder"


def upgrade() -> None:
    op.alter_column(
        TABLE,
        COLUMN,
        existing_type=sa.String(),
        nullable=True,
        schema=SCHEMA,
    )


def downgrade() -> None:
    null_rows = op.get_bind().execute(
        sa.text(
            f"SELECT count(*) FROM {SCHEMA}.{TABLE} "  # noqa: S608
            f"WHERE {COLUMN} IS NULL"  # noqa: S608
        )
    ).scalar_one()
    if null_rows:
        raise RuntimeError(
            "cannot restore a required reply placeholder while "
            f"{null_rows} approval rows have no placeholder"
        )

    op.alter_column(
        TABLE,
        COLUMN,
        existing_type=sa.String(),
        nullable=False,
        schema=SCHEMA,
    )
