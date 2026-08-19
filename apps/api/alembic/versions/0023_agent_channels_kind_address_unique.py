"""binding uniqueness widens from address to (kind, address) (#1459)

Migration 0021 put uniqueness on `address` ALONE and said why: the queue wire
carried no kind, so a pair-unique constraint would let two agents hold one address
while the resolver, seeing only the address, could not tell them apart -- #38's
silent misrouting wearing a different hat. ADR-0096 phase 2 removes that reason.
`ReplyHandle.kind` is now required on the wire and `binding._RESOLVE_SQL` matches
on `c.kind = :kind AND c.address = :address`, so the pair is the routing key and
the constraint follows it.

**Ordering is load-bearing, not incidental.** This migration is the one that makes
an OLD address-only worker dangerous rather than merely stale: it is what permits
two kinds to share an address, and an old worker reading a new turn drops the kind
silently (`extra="ignore"`) and routes on the address alone. The cutover checklist
runs this only after proving zero old worker pods are running.

**The constraint's NAME is a contract**, following 0021's discipline for
`agents_slack_channel_key`: `routers/agents.py` keys its 409 message map on the
literal name, so a constraint created under a Postgres-generated name has the
right shape and the wrong identity, turning #38's actionable conflict into an
opaque 500.

`downgrade` PRE-FLIGHTS and refuses by name when any address is bound under more
than one kind. Restoring an address-only constraint over such a database has no
honest answer -- one of the two agents would have to lose its binding -- and the
alternative is a bare `duplicate key value violates unique constraint` naming one
row and leaving the operator to find the other.

`agent_channels_agent_id_key` is deliberately NOT touched: one agent still binds
one channel (ADR-0089).

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "agent_channels"

# Both names are spelled out because the API's 409 map reads them: the old one so
# the drop and the downgrade's re-create are legible as inverses, the new one so
# the map's key and the catalog's entry cannot drift apart.
OLD_CONSTRAINT = "agent_channels_address_key"
NEW_CONSTRAINT = "agent_channels_kind_address_key"


def upgrade() -> None:
    op.drop_constraint(OLD_CONSTRAINT, TABLE, type_="unique", schema=SCHEMA)
    op.create_unique_constraint(NEW_CONSTRAINT, TABLE, ["kind", "address"], schema=SCHEMA)


def downgrade() -> None:
    conn = op.get_bind()

    shared = conn.execute(
        sa.text(
            f"""
            SELECT address, string_agg(DISTINCT kind, ', ' ORDER BY kind) AS kinds
            FROM {SCHEMA}.{TABLE}
            GROUP BY address
            HAVING count(DISTINCT kind) > 1
            ORDER BY address
            """
        )
    ).all()
    if shared:
        detail = "; ".join(f"{row.address} (kinds: {row.kinds})" for row in shared)
        raise RuntimeError(
            "cannot narrow binding uniqueness back to the address alone (#1459): "
            f"these addresses are bound under more than one kind -- {detail}. An "
            "address-only constraint cannot represent them, and an address-only "
            "resolver could not tell them apart even if it could, so one agent "
            "would be silently shadowed (#38). Move or delete all but one binding "
            "per address, then re-run this downgrade."
        )

    op.drop_constraint(NEW_CONSTRAINT, TABLE, type_="unique", schema=SCHEMA)
    op.create_unique_constraint(OLD_CONSTRAINT, TABLE, ["address"], schema=SCHEMA)
