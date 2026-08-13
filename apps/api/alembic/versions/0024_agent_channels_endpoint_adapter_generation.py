"""a binding gets its server-controlled route and its rebind generation (#1459)

`endpoint` is where this kind's replies go back through, `adapter` names the
egress identity whose credential authenticates them, and `generation` counts
rebinds. All three are platform-set: an ingress request body never supplies them.

The backfill is trivially all-NULL/zero, because no binding has a route today and
a NULL endpoint means "the worker's configured default", which is exactly today's
behavior for Slack. That is also why the cutover has to reconfigure every non-Slack
binding before ingress restarts: each one comes out of this migration UNROUTABLE.

`generation` is NOT NULL at 0 rather than nullable: a NULL generation cannot be
compared against a credential's claim, so a nullable column would make the rebind
check silently vacuous for every pre-0024 row. Its `server_default` of 0 STAYS
(unlike 0022's `reply_kind`, which has none): zero is the honest starting count
for any binding, including one written out of band, whereas a fabricated channel
kind is a lie. Nothing is lost by defaulting a counter that begins at zero.

**`agent_channels_route_pair_ck` states the pair invariant at the DATABASE**, not
only in `ChannelBinding`. The two columns are independently nullable, so a
half-configured route is representable, and a row written out of band (a restored
dump, an operator's psql, a future code path) would pass ingress and then fail
inside the worker, far from its cause. The CHECK is deliberately weaker than the
application rule -- it says only that both are set or neither is, never that a
non-Slack kind REQUIRES both. The database states the invariant true for every
kind; the schema layer states the kind-specific policy. A CHECK that knew what
`slack` meant would be the schema layer, in the wrong place.

`downgrade` REFUSES in TWO directions, both of which the drop would destroy
silently:

1. any binding carrying a non-NULL `endpoint` or `adapter` -- dropping those
   columns destroys the only record of where a live adapter's traffic goes and
   which credential authenticates it, a re-upgrade cannot reconstruct either
   (this backfill is all-NULL by construction), and a worker running against a
   downgraded schema fails closed on every non-Slack turn; and
2. any binding whose `generation` is nonzero -- a token claim binds
   `(channel_id, generation)`, and `update_agent_binding` mutates the row in
   place, so the id is a STABLE identity and the generation is the only thing
   that revokes a credential minted before a rebind. Dropping the column and
   re-upgrading resets every counter to 0, which makes every previously revoked
   generation-0 token valid again against the same binding id: revocation
   undone, with no trace in either schema.

When nothing is routed and every generation is 0 it drops in REVERSE CREATION
ORDER -- the CHECK first, then `generation`, `adapter`, `endpoint` -- so the
constraint never outlives a column it references, and no orphaned constraint
blocks a re-upgrade's own ADD CONSTRAINT.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "agent_channels"

# Named explicitly, following 0021's discipline, so the API can translate it and
# the test can look it up by identity rather than by shape.
ROUTE_PAIR_CHECK = "agent_channels_route_pair_ck"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("endpoint", sa.String(), nullable=True), schema=SCHEMA)
    op.add_column(TABLE, sa.Column("adapter", sa.String(), nullable=True), schema=SCHEMA)
    op.add_column(
        TABLE,
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )

    op.create_check_constraint(
        ROUTE_PAIR_CHECK,
        TABLE,
        "(endpoint IS NULL) = (adapter IS NULL)",
        schema=SCHEMA,
    )


def downgrade() -> None:
    conn = op.get_bind()

    routed = conn.execute(
        sa.text(
            f"""
            SELECT kind, address, endpoint, adapter
            FROM {SCHEMA}.{TABLE}
            WHERE endpoint IS NOT NULL OR adapter IS NOT NULL
            ORDER BY kind, address
            """
        )
    ).all()
    if routed:
        detail = "; ".join(
            f"{row.kind}:{row.address} (endpoint {row.endpoint}, adapter {row.adapter})"
            for row in routed
        )
        raise RuntimeError(
            "cannot drop the binding route columns (#1459): these bindings would "
            f"lose the only record of where their replies go -- {detail}. A "
            "re-upgrade cannot reconstruct them (this migration's backfill is "
            "all-NULL by construction), and a worker running against the narrowed "
            "schema fails closed on every non-Slack turn. Record these routes "
            "elsewhere, clear them, then re-run this downgrade."
        )

    # A nonzero generation is a REVOCATION record, not merely a counter: a token
    # claim binds (channel_id, generation), and the row id survives every rebind,
    # so dropping the column and re-upgrading (which restores it at 0) makes every
    # already-revoked generation-0 token authoritative again for that same id.
    # Refused in the same style as the route columns above: destroying a durable
    # security fact is never a silent step.
    rebound = conn.execute(
        sa.text(
            f"""
            SELECT kind, address, generation
            FROM {SCHEMA}.{TABLE}
            WHERE generation <> 0
            ORDER BY kind, address
            """
        )
    ).all()
    if rebound:
        detail = "; ".join(
            f"{row.kind}:{row.address} (generation {row.generation})" for row in rebound
        )
        raise RuntimeError(
            "cannot drop agent_channels.generation (#1459): these bindings have "
            f"been rebound and their generation is the record of it -- {detail}. A "
            "token claim binds (channel_id, generation), and the binding id is "
            "stable across a rebind, so dropping this column and re-upgrading "
            "resets every counter to 0 and RESURRECTS the credentials those "
            "rebinds revoked. Rotate the affected bindings' credentials and record "
            "their generations elsewhere, reset them to 0, then re-run this "
            "downgrade."
        )

    # Reverse creation order: the CHECK references both columns it is dropped
    # ahead of, and a constraint left behind would block the re-upgrade.
    op.drop_constraint(ROUTE_PAIR_CHECK, TABLE, type_="check", schema=SCHEMA)
    op.drop_column(TABLE, "generation", schema=SCHEMA)
    op.drop_column(TABLE, "adapter", schema=SCHEMA)
    op.drop_column(TABLE, "endpoint", schema=SCHEMA)
