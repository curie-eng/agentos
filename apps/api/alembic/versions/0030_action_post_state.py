"""What an action left behind, so a restore can tell whether the world moved.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-25

ADR-0117 decision 4 refuses a restore when the resource no longer looks like what
the action left. Nothing recorded that: 0029 holds ``prior_state`` (where the
resource came FROM) and ``result``, and neither answers where the action PUT it.

Deriving it from ``arguments`` is not available, and not merely inconvenient: a
PATCH's result is not its request body, and deriving a reversal from the forward
call's arguments is the mapping-DSL alternative ADR-0117 rejects. It comes from
the connector's reply, like the prior state.

Nullable with no backfill. Rows written before a connector reports a post-state
have none, and an undo on such a row is refused for want of something to compare
against -- deny-by-default, the same posture the rest of the ledger takes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "agent_actions",
        sa.Column("post_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agent_actions", "post_state", schema=SCHEMA)
