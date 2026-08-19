"""reconcile a v0.6.x database that never ran 0021_agent_channels (#1705)

Revision id `0021` means two different things across the release train. On the
v0.6.x line it is `0021_console_sessions`; on this line it is
`0021_agent_channels`, and console_sessions was renumbered to `0026`. Both sides
declare `down_revision = "0020"`, so a v0.6.x database is already stamped `0021`
meaning "console_sessions applied". This chain reads that stamp, concludes
`0021_agent_channels` has run, skips it, and starts at `0022`, which joins a
`curie.agent_channels` that was never created.

Renumbering is not available as a fix. v0.6.2 is published and operator
databases carry `0021` meaning console_sessions, so every revision id that has
shipped has to keep the meaning it shipped with. This revision sits between
`0021` and `0022` instead and reconciles whichever of the two meanings the
database in front of it actually carries: on a fresh install `agent_channels` is
already there and there is nothing to do, and on a v0.6.x database the binding
table, its constraints and its backfill all still have to land before `0022` can
read them.

It executes `0021_agent_channels.upgrade()` rather than restating that
migration's statements. The two paths have to converge on the same schema, and
executing the same statements is the only way to guarantee they do: a second
copy of the CREATE TABLE, the two named unique constraints and the
`INSERT ... SELECT` would be free to diverge from the original.

That matters most for the half that is not DDL. 0021's own docstring records
that the BACKFILL is the migration, and that creating the table without the
`INSERT ... SELECT` produces a perfectly valid empty table and silently unbinds
every agent in the install: deployed, healthy looking, answering nothing. A
reconciliation that created an empty table would be worse than the crash it
replaces, because a crash stops the rollout and an empty table does not.

`downgrade` is deliberately a no-op. 0021 still sits below this revision and
still owns the inverse of its own upgrade, so undoing the work here as well
would leave that downgrade with nothing to drop. Walking down through this
revision therefore lands on the v0.7 shape of `0020`, which is where the chain
below it expects to be.

Revision ID: 0021a
Revises: 0021
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021a"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
TABLE = "agent_channels"

# The revision whose effect this one replays. Named rather than resolved
# relative to this revision: a later insertion above 0021 would silently
# re-point a relative reference at the wrong migration.
AGENT_CHANNELS_REVISION = "0021"


def upgrade() -> None:
    already_applied = (
        op.get_bind().execute(sa.text(f"SELECT to_regclass('{SCHEMA}.{TABLE}')")).scalar()
        is not None
    )
    if already_applied:
        return

    script = op.get_context().script
    if script is None:
        raise RuntimeError(
            "cannot reconcile a v0.6.x database (#1705): this revision replays "
            f"{AGENT_CHANNELS_REVISION} and needs the alembic script directory to reach "
            "it, but the migration context was configured without one. Run the upgrade "
            "through the alembic command API (`alembic upgrade head`) rather than a "
            "bare MigrationContext."
        )

    script.get_revision(AGENT_CHANNELS_REVISION).module.upgrade()


def downgrade() -> None:
    """No-op: 0021 below still owns the inverse of the work replayed here."""
