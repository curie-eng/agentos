"""agents.hook_partitions: which hooks fan out, and by what (ADR-0134)

ADR-0079's inbound hook ingress gives every firing of one hook a single thread.
This column is the operator's opt-in to narrowing that to one thread per
PARTITION: it holds a JSONB map of hook name -> ``{"pointer": <RFC 6901>}``,
where the pointer names the field of a delivery body that identifies the thing
the delivery is about (a pull request number, a ticket key).

The map lives on the agent row rather than travelling on the request because a
partition named by the sender would sit outside the bytes the delivery's HMAC
covers, and would hand whoever holds the hook secret this agent's sandbox
cardinality. Nothing here is a credential: a pointer is configuration, and the
value it reads never lands in this table.

Nullable with no server default, because NULL IS the unpartitioned posture.
Every existing agent reads as NULL and keeps minting the byte-identical
three-segment conversation id it minted before, so the column needs no backfill
and no row is rewritten.

Numbered 0036 rather than 0035: 0035 is claimed by another migration in flight on
this release train, and a duplicate revision id across the train breaks every
operator upgrade silently, while a fork in the history is caught loudly by the
head check.

This revises 0034 for that reason, so whichever of the two merges SECOND must
re-parent its own migration onto the other's head before it merges. Do not
rewrite this ``down_revision`` once it has merged: a database already stamped at
0036 will never retroactively run a 0035 inserted beneath it, and the one-head
check cannot see that hole.

Revision ID: 0036
Revises: 0034
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "hook_partitions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Dropping the column silently returns every configured hook to one thread
    # per hook: the deliveries that were fanning out start serializing again, and
    # each partition's transcript is orphaned rather than continued. A downgrade
    # here is a behavior event, not just a schema change, and the configuration
    # it drops cannot be recovered from anywhere else.
    op.drop_column("agents", "hook_partitions", schema=SCHEMA)
