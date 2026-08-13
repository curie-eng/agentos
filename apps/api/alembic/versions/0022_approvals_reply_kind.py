"""approvals carry their own routing half: reply_kind + reply_adapter (#1459)

The resume turn is rebuilt from the approval row, possibly days after it was
raised and possibly after an operator re-pointed the binding. So the channel KIND
has to be a recorded fact about the ORIGINAL turn, not something re-derived from
`agent_channels` at resume time -- re-deriving it is the silent misroute ADR-0096
phase 2 exists to close, at its least observable point.

`reply_adapter` rides the same revision: the durable twin of
`ReplyHandle.adapter`, plain nullable with NO backfill. Every approval predating
this migration was Slack-routed or is unreconstructable, and Slack legitimately
has no adapter, so NULL is the correct and only honest value.

**The backfill is BY PROVENANCE, and refuses rather than guesses.** Each approval
joins `agent_channels` on `approvals.reply_channel = agent_channels.address`. A
row whose address resolves to exactly one binding takes that binding's kind. A row
resolving to zero bindings, or to more than one kind, is UNRECONSTRUCTABLE:

* still PENDING -> raise, naming every offending id and its `reply_channel`. A
  pending approval carries a live resume obligation, and there is deliberately no
  force-through flag; the operator's next step is to resolve those approvals.
* TERMINAL (approved/rejected/expired) -> `'slack'`, logged. It owes no wake, so
  its kind can never misroute anything, and refusing on it would block the cutover
  on rows that cannot hurt anyone.

**Stated rather than papered over: this preflight decides on the CURRENT binding,
and the current binding is mutable.** `crud.update_agent_binding` rewrites kind and
address in place, so an approval raised when the address was bound to `email` and
since re-pointed to `webhook` preflights as `webhook`. There is no historical
record to consult -- that gap is precisely what `reply_kind` closes going forward,
and it cannot be closed retroactively for rows written before the column existed.
Detectable ambiguity aborts; undetectable staleness is documented residual risk,
mitigated by the cutover's operator-confirmation step, which is why every pending
backfill is logged with its id, address and chosen kind.

`downgrade` REFUSES in two directions, because dropping the column destroys
durable routing identity for exactly the rows that need it:

1. any approval carrying a non-`'slack'` kind -- obviously unrepresentable; and
2. any `'slack'` approval whose `reply_channel` is CURRENTLY bound to a different
   kind. After the drop a resume would re-derive the kind from the binding and get
   the wrong one: the same silent misroute in the opposite direction.

The add-nullable / backfill / set-not-null shape is migration 0021's own, for the
reason it states: adding a NOT NULL column to a populated table has no value to
put in the existing rows. No `server_default` is left behind, deliberately -- an
old API pod inserting during the window must fail loudly rather than fabricate a
kind.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-13
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "approvals"
CHANNELS = "agent_channels"

# The only kind a row with no usable provenance can honestly take, and only when
# it is terminal. Inventing it for a pending row is the failure this refuses on.
SLACK = "slack"

# Statuses that owe no future wake. Kept as a literal tuple rather than imported
# from the app: a migration is a historical record and must not change meaning
# when `ApprovalStatus` grows a member.
TERMINAL_STATUSES = ("approved", "rejected", "expired")

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()

    # 0. The adapter selector: nullable, no backfill, no constraint.
    op.add_column(TABLE, sa.Column("reply_adapter", sa.String(), nullable=True), schema=SCHEMA)

    # 1. The routing half, nullable for now so existing rows have somewhere to land.
    op.add_column(TABLE, sa.Column("reply_kind", sa.String(), nullable=True), schema=SCHEMA)

    # 2. Preflight. An approval is reconstructable when its address resolves to
    # exactly one KIND -- one kind, not one row: two bindings of the same kind at
    # one address (reachable only out of band) still name a single honest answer.
    unreconstructable = conn.execute(
        sa.text(
            f"""
            SELECT ap.id AS id,
                   ap.reply_channel AS reply_channel,
                   ap.status AS status,
                   count(DISTINCT c.kind) AS kinds
            FROM {SCHEMA}.{TABLE} ap
            LEFT JOIN {SCHEMA}.{CHANNELS} c ON c.address = ap.reply_channel
            GROUP BY ap.id, ap.reply_channel, ap.status
            HAVING count(DISTINCT c.kind) <> 1
            ORDER BY ap.id
            """
        )
    ).all()

    # 3. Refuse on ambiguity that still matters: a PENDING row.
    blocking = [row for row in unreconstructable if row.status not in TERMINAL_STATUSES]
    if blocking:
        detail = "; ".join(
            f"{row.id} (reply_channel {row.reply_channel!r}: "
            f"{'no binding' if row.kinds == 0 else f'{row.kinds} kinds'})"
            for row in blocking
        )
        raise RuntimeError(
            "cannot backfill approvals.reply_kind (#1459): these approvals are "
            f"still pending and their channel kind cannot be established -- {detail}. "
            "A pending approval carries a live resume obligation, and guessing its "
            "kind would deliver its reply through the wrong adapter, which is the "
            "silent misroute this column exists to prevent. Resolve or expire these "
            "approvals (POST /approvals/{id}/resolve), then re-run this migration."
        )

    for row in unreconstructable:
        logger.warning(
            "approval %s (reply_channel %r) has no reconstructable channel kind; "
            "it is terminal (%s) so it can never be resumed -- backfilling %r",
            row.id,
            row.reply_channel,
            row.status,
            SLACK,
        )

    # The provenance backfill: each row takes the kind of the binding its OWN
    # address resolves to. Everything left over is terminal by the check above.
    conn.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.{TABLE} ap
            SET reply_kind = sub.kind
            FROM (
                SELECT ap2.id AS id, min(c.kind) AS kind
                FROM {SCHEMA}.{TABLE} ap2
                JOIN {SCHEMA}.{CHANNELS} c ON c.address = ap2.reply_channel
                GROUP BY ap2.id
                HAVING count(DISTINCT c.kind) = 1
            ) AS sub
            WHERE sub.id = ap.id
            """
        )
    )
    conn.execute(
        sa.text(
            f"UPDATE {SCHEMA}.{TABLE} SET reply_kind = :kind WHERE reply_kind IS NULL"
        ),
        {"kind": SLACK},
    )

    # Every pending row that was backfilled is logged for the cutover's
    # operator-confirmation step: the preflight cannot distinguish a correct match
    # from an address that was rebound since the approval was raised.
    pending = conn.execute(
        sa.text(
            f"""
            SELECT id, reply_channel, reply_kind, status
            FROM {SCHEMA}.{TABLE}
            ORDER BY id
            """
        )
    ).all()
    for row in (r for r in pending if r.status not in TERMINAL_STATUSES):
        logger.info(
            "approval %s (reply_channel %r) backfilled to reply_kind %r; confirm this "
            "is the kind it was RAISED on, not merely the kind its address holds now",
            row.id,
            row.reply_channel,
            row.reply_kind,
        )

    # 4. Tighten. No server_default: an insert that omits the kind must fail.
    op.alter_column(TABLE, "reply_kind", nullable=False, schema=SCHEMA)


def downgrade() -> None:
    conn = op.get_bind()

    unreconstructable = conn.execute(
        sa.text(
            f"""
            SELECT ap.id AS id,
                   ap.reply_channel AS reply_channel,
                   ap.reply_kind AS reply_kind,
                   c.kind AS bound_kind
            FROM {SCHEMA}.{TABLE} ap
            LEFT JOIN {SCHEMA}.{CHANNELS} c ON c.address = ap.reply_channel
            WHERE ap.reply_kind <> :kind
               OR (c.kind IS NOT NULL AND c.kind <> ap.reply_kind)
            ORDER BY ap.id
            """
        ),
        {"kind": SLACK},
    ).all()
    if unreconstructable:
        detail = "; ".join(
            f"{row.id} (reply_channel {row.reply_channel!r}: raised on "
            f"{row.reply_kind!r}, address now bound to "
            f"{'nothing' if row.bound_kind is None else repr(row.bound_kind)})"
            for row in unreconstructable
        )
        raise RuntimeError(
            "cannot drop approvals.reply_kind (#1459): these approvals would lose "
            f"the only record of the channel they were raised on -- {detail}. After "
            "the drop a resume re-derives the kind from the CURRENT binding, so each "
            "of these would either have no kind at all or be delivered through the "
            "wrong adapter. Resolve them, or re-point the bindings back, then re-run "
            "this downgrade."
        )

    op.drop_column(TABLE, "reply_kind", schema=SCHEMA)
    op.drop_column(TABLE, "reply_adapter", schema=SCHEMA)
