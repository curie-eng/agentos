"""Which approval gated a recorded call, so an undo can require the same route.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-25

Renumbered from 0031 with its sibling; see 0032_action_post_state.py.

ADR-0117 decision 3: an undo requires the authorization the forward action
required, and no more. Answering that needs to know whether the call was gated at
all, and by which route -- which the approval row holds.

Deliberately NOT a foreign key. This column is the record of what authorization
the forward action required; an ON DELETE SET NULL would let the approval
sweeper silently downgrade a gated action to an ungated one, which is a
permission check quietly deleting itself. Stored as a bare id, so an approval
that can no longer be read fails the undo closed instead.

Nullable with no backfill: rows written before this, and calls that were never
gated, both carry NULL, and NULL means ungated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "agent_actions",
        sa.Column("gate_approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agent_actions", "gate_approval_id", schema=SCHEMA)
