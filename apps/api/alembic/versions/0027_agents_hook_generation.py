"""agents.hook_generation: the rotation counter behind derived hook secrets (#269)

ADR-0079's inbound hook ingress needs a per-agent shared secret an upstream signs
its deliveries with. That secret is DERIVED (``hook_secret.derive``) from the
platform key, the agent id and this counter rather than stored, so no
third-party credential sits in plaintext in the control plane and the existing
production guard on ``API_KEY`` covers it for free.

What this column adds is the ability to revoke ONE agent's hook secret. Without
it the only way to invalidate a leaked secret would be rotating the platform key,
which invalidates every credential the platform has ever minted. The column
holds an ordinary integer and reading it grants nothing.

Defaults to 0 for existing rows, which is the same value a newly created agent
gets, so no agent has a "no secret yet" state to special-case.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    # NOT NULL with a server default, so the backfill and the constraint land in
    # one statement and no window exists where an existing agent reads as NULL.
    op.add_column(
        "agents",
        sa.Column(
            "hook_generation",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Dropping the counter resets every agent to generation 0 on a later upgrade,
    # which REVIVES any hook secret that was rotated away. A downgrade here is
    # therefore a credential event, not just a schema change.
    op.drop_column("agents", "hook_generation", schema=SCHEMA)
