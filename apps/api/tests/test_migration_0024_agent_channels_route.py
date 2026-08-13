"""Migration 0024 gives a binding its server-controlled route and its generation.

`0024_agent_channels_endpoint_adapter_generation.py` adds `endpoint` (nullable),
`adapter` (nullable), `generation` (NOT NULL, default 0), and the CHECK
constraint `agent_channels_route_pair_ck` enforcing `(endpoint IS NULL) =
(adapter IS NULL)`.

Why the CHECK exists at all, given `ChannelBinding` already validates the pair
(plan EB-A18): the two columns are independently nullable, so a HALF-configured
route is representable, and a row written out of band -- a restored dump, an
operator's psql, a future code path -- would pass ingress and then fail inside
the worker, far from its cause. The DB states the invariant true for every kind;
the schema layer states the kind-specific requirement (a non-Slack binding needs
BOTH). Two layers, one for representability and one for policy, rather than a
CHECK that has to know what `slack` means.

Why the downgrade refuses: dropping these columns destroys the only record of
where a live adapter's traffic goes and which credential authenticates it. A
re-upgrade cannot reconstruct either (0024's backfill is all-NULL by
construction), and a worker running against a downgraded schema fails closed on
every non-Slack turn.

Follows `test_migration_0021_agent_channels.py`: a throwaway database all to
itself via `isolated_migration_db`, real Postgres, no mocking.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from curie_api.config import get_settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

# Targeted explicitly, never as a relative "-1" (#1391).
BELOW = "0023"
REVISION = "0024"

ROUTE_PAIR_CHECK = "agent_channels_route_pair_ck"
ROUTE_COLUMNS = ("endpoint", "adapter", "generation")


def _sql(statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def _go() -> list[Any]:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params or {})
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return cfg


def _at_below() -> Config:
    cfg = _alembic_config()
    command.upgrade(cfg, BELOW)
    return cfg


def _constraint_named(name: str) -> bool:
    rows = _sql(
        "SELECT 1 FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'curie' AND c.conname = :name",
        {"name": name},
    )
    return bool(rows)


def _columns() -> set[str]:
    rows = _sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'agent_channels'"
    )
    return {row[0] for row in rows}


def _seed_agent(name: str) -> uuid.UUID:
    agent_id = uuid.uuid4()
    _sql(
        "INSERT INTO curie.agents (id, name) VALUES (:id, :name)",
        {"id": agent_id, "name": name},
    )
    return agent_id


def _insert_binding(
    name: str,
    kind: str,
    address: str,
    *,
    endpoint: str | None = None,
    adapter: str | None = None,
) -> uuid.UUID:
    """A raw `agent_channels` INSERT -- deliberately NOT through the API.

    Out-of-band writes are the whole reason the database-level invariant exists
    (T-A16): the application validator cannot see them.
    """

    agent_id = _seed_agent(name)
    _sql(
        "INSERT INTO curie.agent_channels (id, agent_id, kind, address, endpoint, adapter) "
        "VALUES (:id, :agent, :kind, :addr, :endpoint, :adapter)",
        {
            "id": uuid.uuid4(),
            "agent": agent_id,
            "kind": kind,
            "addr": address,
            "endpoint": endpoint,
            "adapter": adapter,
        },
    )
    return agent_id


def _seed_slack_binding(name: str, address: str) -> uuid.UUID:
    agent_id = _seed_agent(name)
    _sql(
        "INSERT INTO curie.agent_channels (id, agent_id, kind, address) "
        "VALUES (:id, :agent, 'slack', :addr)",
        {"id": uuid.uuid4(), "agent": agent_id, "addr": address},
    )
    return agent_id


def test_the_upgrade_adds_the_route_columns_with_a_null_backfill(
    isolated_migration_db: None,
) -> None:
    """AC13c, upgrade half. The backfill is trivially all-NULL/zero (no binding
    has a route today), which is exactly why cutover steps 8-11 exist: every
    non-Slack binding comes out of this migration UNROUTABLE and must be
    reconfigured before ingress restarts.

    `generation` is NOT NULL at 0 rather than nullable: a NULL generation cannot
    be compared against a token's claim, so a nullable column would make D5's
    rebind check silently vacuous for pre-0024 rows.
    """

    cfg = _at_below()
    _seed_slack_binding("slack-agent", "C0ROUTE01")

    command.upgrade(cfg, REVISION)

    assert set(ROUTE_COLUMNS) <= _columns()
    rows = _sql(
        "SELECT endpoint, adapter, generation FROM curie.agent_channels "
        "WHERE address = 'C0ROUTE01'"
    )
    assert rows == [(None, None, 0)]

    nullable = _sql(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'curie' AND table_name = 'agent_channels' "
        "AND column_name = ANY(:cols)",
        {"cols": list(ROUTE_COLUMNS)},
    )
    assert dict(nullable) == {"endpoint": "YES", "adapter": "YES", "generation": "NO"}


@pytest.mark.parametrize(
    ("endpoint", "adapter"),
    [
        ("http://curie-mail-adapter:8080/", None),
        (None, "agentmail-sandbox"),
    ],
)
def test_a_half_configured_route_is_rejected_by_the_database(
    isolated_migration_db: None, endpoint: str | None, adapter: str | None
) -> None:
    """T-A16 / AC13b. Both directions of the pair, because a CHECK written as
    `endpoint IS NOT NULL OR adapter IS NULL` catches one and waves the other
    through.

    The reviewer's failure mode was a row that "accepts ingress and fails later
    in the worker" -- a route half-written by hand or by a restored dump. Only a
    database-level invariant forecloses it for rows the application never sees,
    which is why T-C11/T-C12 (the schema half) are not sufficient on their own.
    """

    cfg = _at_below()
    command.upgrade(cfg, REVISION)

    with pytest.raises(Exception) as caught:
        _insert_binding(
            "half-routed", "email", "ops@example.test", endpoint=endpoint, adapter=adapter
        )
    assert ROUTE_PAIR_CHECK in str(caught.value), caught.value


def test_both_set_and_both_absent_are_accepted(isolated_migration_db: None) -> None:
    """T-A16's positive control. Without it, a CHECK of `false` would pass the
    two rejection cases above and make every binding uninsertable.
    """

    cfg = _at_below()
    command.upgrade(cfg, REVISION)

    _insert_binding(
        "fully-routed",
        "email",
        "ops@example.test",
        endpoint="http://curie-mail-adapter:8080/",
        adapter="agentmail-sandbox",
    )
    # Slack's route is legitimately implicit: the worker's configured origin.
    _insert_binding("slack-agent", "slack", "C0ROUTE02")

    assert len(_sql("SELECT 1 FROM curie.agent_channels")) == 2
    assert _constraint_named(ROUTE_PAIR_CHECK)


def test_the_round_trip_succeeds_on_a_slack_only_database(
    isolated_migration_db: None,
) -> None:
    """T-A14, round-trip half / AC13c.

    upgrade -> downgrade -> upgrade with no binding carrying a route. The
    ordering trap this catches: the CHECK constraint references `endpoint` and
    `adapter`, so a downgrade that drops the columns before the constraint fails
    outright (or, worse, leaves a constraint behind that blocks the re-upgrade's
    own ADD CONSTRAINT). Reverse creation order is the fix, and the second
    upgrade is what proves it.
    """

    cfg = _at_below()
    _seed_slack_binding("slack-agent", "C0ROUTE03")

    command.upgrade(cfg, REVISION)
    command.downgrade(cfg, BELOW)

    assert not _constraint_named(ROUTE_PAIR_CHECK)
    assert _columns().isdisjoint(ROUTE_COLUMNS)
    # The binding itself is untouched: a downgrade drops the route, never the row.
    assert len(_sql("SELECT 1 FROM curie.agent_channels")) == 1

    command.upgrade(cfg, REVISION)
    assert _constraint_named(ROUTE_PAIR_CHECK)
    assert set(ROUTE_COLUMNS) <= _columns()


def test_the_downgrade_refuses_and_names_any_routed_binding(
    isolated_migration_db: None,
) -> None:
    """T-A14, refusal half / AC13c.

    Mutation this catches: make `downgrade` an unconditional `drop_column`. The
    round-trip test above still passes (it has no routed binding), and the only
    signal is a production database that has silently forgotten where its email
    adapter lives and which credential authenticates the platform to it.
    """

    cfg = _at_below()
    command.upgrade(cfg, REVISION)
    _insert_binding(
        "mail-agent",
        "email",
        "ops@example.test",
        endpoint="http://curie-mail-adapter:8080/",
        adapter="agentmail-sandbox",
    )
    _seed_slack_binding("slack-agent", "C0ROUTE04")

    with pytest.raises(Exception) as caught:
        command.downgrade(cfg, BELOW)

    message = str(caught.value)
    assert "ops@example.test" in message, message
    assert "agentmail-sandbox" in message or "curie-mail-adapter" in message, message
    # The unrouted slack binding is not blamed for its neighbour's state.
    assert "C0ROUTE04" not in message, message
    # And the refusal was total: the route survives.
    assert _sql(
        "SELECT adapter FROM curie.agent_channels WHERE address = 'ops@example.test'"
    ) == [("agentmail-sandbox",)]


def test_the_downgrade_refuses_a_rebound_binding_whose_generation_is_nonzero(
    isolated_migration_db: None,
) -> None:
    """T-A14, the revocation half.

    An unrouted Slack binding passes the route predicate, and its generation is
    still a durable SECURITY fact: a token claim binds `(channel_id, generation)`
    and `update_agent_binding` mutates the row in place, so the id survives every
    rebind. Drop the column and re-upgrade (which restores it at 0) and every
    credential those rebinds revoked is authoritative again for the same binding
    id -- revocation silently undone.

    Mutation this catches: delete the generation predicate. Every other test in
    this file still passes (they all sit at generation 0), and the only signal is
    a revoked adapter token that starts working again.
    """

    cfg = _at_below()
    command.upgrade(cfg, REVISION)
    _seed_slack_binding("rebound-agent", "C0ROUTE05")
    _seed_slack_binding("settled-agent", "C0ROUTE06")
    # Two rebinds, exactly as `update_agent_binding` would have counted them.
    _sql(
        "UPDATE curie.agent_channels SET generation = 2 WHERE address = 'C0ROUTE05'"
    )

    with pytest.raises(Exception) as caught:
        command.downgrade(cfg, BELOW)

    message = str(caught.value)
    assert "C0ROUTE05" in message, message
    assert "generation 2" in message, message
    # The never-rebound binding is not blamed for its neighbour's state.
    assert "C0ROUTE06" not in message, message
    # And the refusal was total: the generation survives to be rotated against.
    assert _sql(
        "SELECT generation FROM curie.agent_channels WHERE address = 'C0ROUTE05'"
    ) == [(2,)]
