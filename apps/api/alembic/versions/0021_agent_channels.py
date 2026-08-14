"""channel-neutral agent binding: agent_channels replaces agents.slack_channel (#1459)

`agents.slack_channel` made the control plane Slack-shaped: binding a channel
kind the platform had never heard of took a schema change, which is the coupling
ADR-0096 removes. The binding moves into `curie.agent_channels` as a neutral
`(kind, address)` pair -- `kind` names the adapter that owns the binding and
selects the address-shape validator, `address` is the routing key the worker
matches on equality.

The BACKFILL is the migration. Creating the table and dropping the column
without the `INSERT ... SELECT` produces a perfectly valid empty table and
silently unbinds every agent in every existing install: deployed,
healthy-looking, answering nothing -- #38's silent-shadow failure at install
scale, which no schema assertion notices.

Uniqueness moves with it, in both directions:

* `agent_channels_address_key` carries migration 0017's `agents_slack_channel_key`
  forward (#38: two agents on one address means the worker's resolver picks one
  and shadows the rest). On ADDRESS ALONE, not on `(kind, address)`: the queue
  wire carries no kind today, so a pair-unique constraint would let two agents
  hold one address while the resolver, which sees only the address, could not
  tell them apart. The pair lands with the wire field, not before.
* `agent_channels_agent_id_key` re-establishes what the scalar column got for
  free (ADR-0089: "one agent still binds one channel"). A child table discards
  that silently, and nothing fails until an operator binds a second channel and
  finds one of them dead.

Both directions pre-flight rather than assume. 0017 already guarantees address
uniqueness and 0001 makes the column NOT NULL, so the backfill provably cannot
collide or produce a NULL -- but "provably" rests on 0017 having run, and a
database restored from a pre-0017 dump has not. `downgrade` has the harder job:
the new table can hold states a single NOT NULL string column cannot represent
(a non-`slack` kind, an agent with no binding at all), and the only honest answer
is to refuse by name. The alternatives are relabelling a webhook address as a
Slack channel, or failing with a bare `not-null constraint` error that names a
column and leaves the operator to work out which agent is the problem.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "agent_channels"

# The constraint migration 0017 created on the old column. `downgrade` restores
# it under this EXACT name, not whatever Postgres would generate: the API's 409
# message map is keyed on constraint names, so a generated name turns #38's
# conflict back into an opaque 500, and a re-upgrade's drop would miss it.
LEGACY_CONSTRAINT = "agents_slack_channel_key"
ADDRESS_CONSTRAINT = "agent_channels_address_key"
AGENT_CONSTRAINT = "agent_channels_agent_id_key"

# The only kind the old column could ever have held. Inventing any other kind
# for a backfilled row would be a fabricated fact about an existing install.
SLACK = "slack"


def upgrade() -> None:
    conn = op.get_bind()

    duplicates = conn.execute(
        sa.text(
            f"""
            SELECT slack_channel, count(*) AS n, string_agg(name, ', ' ORDER BY name) AS agents
            FROM {SCHEMA}.agents
            GROUP BY slack_channel
            HAVING count(*) > 1
            ORDER BY slack_channel
            """
        )
    ).all()
    if duplicates:
        detail = "; ".join(
            f"{row.slack_channel}: {row.n} agents ({row.agents})" for row in duplicates
        )
        raise RuntimeError(
            "cannot move the channel binding into agent_channels (#1459): these "
            f"addresses have more than one agent bound -- {detail}. Only one agent "
            "per address can ever respond (the worker's resolver picks prod-first, "
            "then the most recently deployed, and shadows the rest), so the extras "
            "are already inert. Re-point them to their own channels or delete them, "
            "then re-run this migration."
        )

    unbound = conn.execute(
        sa.text(
            f"""
            SELECT name
            FROM {SCHEMA}.agents
            WHERE slack_channel IS NULL OR btrim(slack_channel) = ''
            ORDER BY name
            """
        )
    ).all()
    if unbound:
        detail = ", ".join(row.name for row in unbound)
        raise RuntimeError(
            "cannot move the channel binding into agent_channels (#1459): these "
            f"agents have no channel to move -- {detail}. A binding is required "
            "(an agent with none looks deployed and can never receive a turn), so "
            "give each of them a channel or delete them, then re-run this migration."
        )

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Both NOT NULL: a binding with no kind cannot choose an address rule,
        # and a binding with no address routes nowhere. Neither is a state the
        # write path can produce, so neither is a state this table should hold.
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], [f"{SCHEMA}.agents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("address", name=ADDRESS_CONSTRAINT),
        sa.UniqueConstraint("agent_id", name=AGENT_CONSTRAINT),
        schema=SCHEMA,
    )

    # Exactly one row per agent. `gen_random_uuid()` is a Postgres 13+ builtin,
    # so no extension is needed and no id has to round-trip through Python.
    conn.execute(
        sa.text(
            f"""
            INSERT INTO {SCHEMA}.{TABLE} (id, agent_id, kind, address)
            SELECT gen_random_uuid(), id, :kind, slack_channel
            FROM {SCHEMA}.agents
            """
        ),
        {"kind": SLACK},
    )

    # Drop the constraint explicitly before the column. Postgres would drop it
    # with the column anyway; naming it here keeps the migration's inverse
    # (`create_unique_constraint(LEGACY_CONSTRAINT, ...)` in downgrade) legible
    # as an inverse rather than looking like an invention.
    op.drop_constraint(LEGACY_CONSTRAINT, "agents", type_="unique", schema=SCHEMA)
    op.drop_column("agents", "slack_channel", schema=SCHEMA)


def downgrade() -> None:
    conn = op.get_bind()

    non_slack = conn.execute(
        sa.text(
            f"""
            SELECT a.name, c.kind, c.address
            FROM {SCHEMA}.{TABLE} c
            JOIN {SCHEMA}.agents a ON a.id = c.agent_id
            WHERE c.kind <> :kind
            ORDER BY a.name
            """
        ),
        {"kind": SLACK},
    ).all()
    if non_slack:
        detail = "; ".join(f"{row.name} ({row.kind}: {row.address})" for row in non_slack)
        raise RuntimeError(
            "cannot restore agents.slack_channel (#1459): these agents hold a "
            f"non-slack binding, which that column cannot represent -- {detail}. "
            "Writing the address there anyway would relabel it as a Slack channel, "
            "and the worker would route Slack turns to an agent that never agreed "
            "to serve them. Re-point these agents onto slack bindings or delete "
            "them, then re-run this downgrade."
        )

    orphans = conn.execute(
        sa.text(
            f"""
            SELECT a.name
            FROM {SCHEMA}.agents a
            LEFT JOIN {SCHEMA}.{TABLE} c ON c.agent_id = a.id
            WHERE c.id IS NULL
            ORDER BY a.name
            """
        )
    ).all()
    if orphans:
        detail = ", ".join(row.name for row in orphans)
        raise RuntimeError(
            "cannot restore agents.slack_channel (#1459): these agents have no "
            f"channel binding, and the restored column is NOT NULL -- {detail}. "
            "Without this check the restore fails on a bare not-null violation "
            "that names the column instead of the agent to go fix. Bind or delete "
            "them, then re-run this downgrade."
        )

    # Unreachable while `agent_channels_agent_id_key` stands, and asserted anyway
    # because the constraint is exactly what a later widening drops: the day one
    # agent may hold several bindings, a scalar column can hold only one of them,
    # and choosing silently would drop the rest.
    crowded = conn.execute(
        sa.text(
            f"""
            SELECT a.name, count(*) AS n
            FROM {SCHEMA}.{TABLE} c
            JOIN {SCHEMA}.agents a ON a.id = c.agent_id
            GROUP BY a.name
            HAVING count(*) > 1
            ORDER BY a.name
            """
        )
    ).all()
    if crowded:
        detail = "; ".join(f"{row.name}: {row.n} bindings" for row in crowded)
        raise RuntimeError(
            "cannot restore agents.slack_channel (#1459): these agents hold more "
            f"than one binding, and the column can hold one -- {detail}. Pick the "
            "binding each agent should keep and remove the others, then re-run "
            "this downgrade."
        )

    # Nullable first, then backfilled, then tightened: adding a NOT NULL column
    # to a populated table has no value to put in the existing rows.
    op.add_column("agents", sa.Column("slack_channel", sa.String(), nullable=True), schema=SCHEMA)
    conn.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.agents a
            SET slack_channel = c.address
            FROM {SCHEMA}.{TABLE} c
            WHERE c.agent_id = a.id
            """
        )
    )
    op.alter_column("agents", "slack_channel", nullable=False, schema=SCHEMA)
    op.create_unique_constraint(LEGACY_CONSTRAINT, "agents", ["slack_channel"], schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
