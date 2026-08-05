"""per-agent thinking depth: agents.thinking (#1182)

ADR-0098. A reasoning model reasons before it answers, and until now nothing in
Curie could change that: the runner hands claude-agent-sdk twelve option fields
and none of them is `thinking`, so whatever the model ships with is what runs.
Measured on the endpoint Curie dials, `z-ai/glm-5.2` spends 70% of its output
tokens thinking (8.1s) where `claude-sonnet-4.5` spends none (2.4s); disabling
it takes the same GLM call to 1.7s.

This column is the per-agent half of the two-layer operator control, the exact
shape `agents.model` already has: NULL means the platform default
(`CURIE_THINKING` on the worker) applies, and a value here wins for this agent
only. A bundle has no say at either layer -- it cannot even name its model, and
thinking depth is the same capability-versus-cost axis one notch down.

Nullable with no server default on purpose. NULL is not "off"; it is "this agent
expresses no opinion", which is what every existing row means and what keeps this
migration a no-op for behavior. An agent whose column stays NULL on a worker with
no `CURIE_THINKING` set boots exactly as it does today: the runner sends no
thinking configuration and the model's own default stands.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("thinking", sa.String(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Safe to drop: the column carries operator preference, not agent state, and
    # a re-upgrade simply reads NULL again (the platform default).
    op.drop_column("agents", "thinking", schema=SCHEMA)
