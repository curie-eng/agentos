"""one repository, many agents: drop the unique on agents.repo_full_name (#1070)

ADR-0091. `ix_agents_repo_full_name` was created UNIQUE by 0003, which encoded
"one repository builds one agent" into the schema. That is the last thing
standing between ADR-0090 and its acceptance test: a repo with a dev bot and a
prod bot -- the same bundle on two channels, which is what a dev/prod split of a
Slack bot IS -- cannot exist, so the repository has to keep deploy workflows to
create the second one out of band.

`repo_full_name` becomes what it always described: which repository an agent is
built from, with several agents legitimately sharing one. Which agent a given
push deploys to is answered by the bundle's `deploy.yaml`, not by the schema.

The index itself is kept, non-unique. Git-flow looks agents up by this column on
every webhook delivery, so dropping it outright would turn each push into a
sequential scan of the agents table.

Note the asymmetry with 0017, which ADDED a unique constraint on slack_channel:
one agent still owns one channel. Two agents sharing a repository is intended;
two agents sharing a channel is the silent-shadowing bug 0017 closed. Downgrade
therefore restores the unique index and can fail on data this migration made
legal -- which is the honest behaviour, since ADR-0091 notes re-adding it later
means resolving whatever duplicates exist by then.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"
INDEX = "ix_agents_repo_full_name"


def upgrade() -> None:
    op.drop_index(INDEX, table_name="agents", schema=SCHEMA)
    op.create_index(INDEX, "agents", ["repo_full_name"], unique=False, schema=SCHEMA)


def downgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            f"""
            SELECT repo_full_name, count(*) AS n,
                   string_agg(name, ', ' ORDER BY name) AS agents
            FROM {SCHEMA}.agents
            WHERE repo_full_name IS NOT NULL
            GROUP BY repo_full_name
            HAVING count(*) > 1
            ORDER BY repo_full_name
            """
        )
    ).all()
    if duplicates:
        listed = "; ".join(f"{row.repo_full_name} ({row.agents})" for row in duplicates)
        raise RuntimeError(
            "cannot restore the unique index: these repositories build more than "
            f"one agent -- {listed}. Re-point or delete the extra agents first. "
            "This is the one-way edge ADR-0091 called out."
        )
    op.drop_index(INDEX, table_name="agents", schema=SCHEMA)
    op.create_index(INDEX, "agents", ["repo_full_name"], unique=True, schema=SCHEMA)
