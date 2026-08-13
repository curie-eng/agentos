"""BindingResolver against the REAL compose Postgres (never mocked).

Seeds agents / agent_versions / deployments in the curie schema, then checks
channel resolution, the prod-over-dev preference, unknown-channel -> None, and
the budget/env construction. Rows are namespaced by a per-test token and cleaned
up afterwards.

The one exception is the shadowed-binding test, which needs two agents on a
single channel and so cannot use the curie schema at all. It seeds into a
throwaway schema copied from curie with CREATE TABLE ... (LIKE ...) and cleans up
by dropping that schema; no curie object and no production invariant is touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import unittest.mock
import uuid

import pytest
from curie_worker.binding import (
    APPROVAL_REQUIRED_ENV,
    BUDGET_ENV,
    BUNDLE_REF_ENV,
    CONNECTOR_SECRET_KEYS_ENV,
    HISTORY_TOKEN_ENV,
    MEMORY_TOKEN_ENV,
    BindingResolver,
    ResolvedDeployment,
    warn_if_multiple_agents_bound,
)
from curie_worker.config import WorkerConfig
from curie_worker.sandbox_token import verify
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
)
_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "curie")


async def _seed_agent(
    engine: AsyncEngine,
    *,
    channel: str,
    name: str,
    max_usd: float | None,
    max_tokens: int | None,
    approval_tools: list[str] | None = None,
    approval_routes: dict | None = None,
    secrets: dict | None = None,
    schema: str = _SCHEMA,
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {schema}.agents "
                "(id, name, slack_channel, max_usd_per_day, max_output_tokens_per_run, "
                "approval_required_tools, approval_routes, secrets) "
                "VALUES (:id, :name, :channel, :usd, :tokens, "
                "CAST(:approval_tools AS jsonb), CAST(:approval_routes AS jsonb), "
                "CAST(:secrets AS jsonb))"
            ),
            {
                "id": agent_id,
                "name": name,
                "channel": channel,
                "usd": max_usd,
                "tokens": max_tokens,
                "approval_tools": (
                    json.dumps(approval_tools) if approval_tools is not None else None
                ),
                "approval_routes": (
                    json.dumps(approval_routes) if approval_routes is not None else None
                ),
                "secrets": (json.dumps(secrets) if secrets is not None else None),
            },
        )
    return agent_id


async def _seed_deployment(
    engine: AsyncEngine,
    *,
    agent_id: uuid.UUID,
    environment: str,
    bundle_ref: str,
    status: str = "active",
    schema: str = _SCHEMA,
    environment_type: str = f"{_SCHEMA}.environment",
) -> None:
    version_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {schema}.agent_versions "
                "(id, agent_id, version_label, bundle_ref, created_by) "
                "VALUES (:id, :agent_id, :label, :ref, :by)"
            ),
            {"id": version_id, "agent_id": agent_id, "label": f"v-{environment}",
             "ref": bundle_ref, "by": "test"},
        )
        # The real schema's `environment` column is a `curie.environment` enum,
        # which is why the cast target defaults to that type and stays pinned to
        # `_SCHEMA` rather than following `schema`: the enum exists only in the
        # real schema. The shadowed-binding test's throwaway copied schema passes
        # "text" instead, because it deliberately downgrades its copied column to
        # text so that a schema leaked by a killed run carries no dependency on
        # any curie object.
        await conn.execute(
            text(
                f"INSERT INTO {schema}.deployments "
                "(id, agent_id, version_id, environment, status) "
                "VALUES (:id, :agent_id, :version_id, "
                f"CAST(:env AS {environment_type}), :status)"
            ),
            {"id": uuid.uuid4(), "agent_id": agent_id, "version_id": version_id,
             "env": environment, "status": status},
        )


async def _cleanup(engine: AsyncEngine, agent_ids: list[uuid.UUID]) -> None:
    async with engine.begin() as conn:
        for agent_id in agent_ids:
            await conn.execute(
                text(f"DELETE FROM {_SCHEMA}.agents WHERE id = :id"), {"id": agent_id}
            )


def _resolver(engine: AsyncEngine) -> BindingResolver:
    return BindingResolver(engine, WorkerConfig(db_schema=_SCHEMA))


def test_resolves_channel_to_active_deployment_and_builds_env() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=3.5, max_tokens=4242
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"bundles/{token}.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.agent_id == agent_id
            assert resolved.bundle_ref == f"bundles/{token}.zip"
            assert resolved.max_usd_per_day == 3.5
            assert resolved.max_output_tokens_per_run == 4242

            env = _resolver(engine).boot_env(resolved, "thread-1")
            assert env[BUNDLE_REF_ENV] == f"bundles/{token}.zip"
            assert '"max_usd_per_day":3.5' in env[BUDGET_ENV]
            assert '"max_output_tokens_per_run":4242' in env[BUDGET_ENV]

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_prod_deployment_wins_over_dev() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="dev", bundle_ref="bundles/dev.zip"
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref="bundles/prod.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.bundle_ref == "bundles/prod.zip"  # prod wins

            # NULL budget columns -> platform defaults in the env.
            env = _resolver(engine).boot_env(resolved, "t")
            cfg = WorkerConfig()
            assert f'"max_usd_per_day":{cfg.default_max_usd_per_day}' in env[BUDGET_ENV]

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_second_agent_on_a_bound_channel_is_refused() -> None:
    """#38's invariant, asserted where the worker reads it: the database refuses a
    second agent on an already-bound channel, so the silent-shadow state cannot be
    created through any write path. The API asserts the same invariant as a 409.

    Deliberately does NOT drop the constraint to exercise the shadow branch (#959).
    That earlier form committed a DROP CONSTRAINT against the shared developer
    Postgres and restored it in a `finally`, so a killed process left the
    production invariant absent -- and a concurrent duplicate on any other channel
    could make the restoring ADD CONSTRAINT fail outright. The shadow branch is now
    covered in two halves: its logic by
    `test_warn_if_multiple_agents_bound_names_the_shadowed_agent`, which needs no
    database at all, and its invocation by
    `test_resolve_warns_when_two_agents_are_bound_to_one_channel`, which does use a
    database but seeds a throwaway copied schema and so never removes the
    constraint asserted here.
    """

    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-a-{token}", max_usd=None, max_tokens=None
            )
            try:
                with pytest.raises(IntegrityError):
                    await _seed_agent(
                        engine,
                        channel=channel,
                        name=f"agent-b-{token}",
                        max_usd=None,
                        max_tokens=None,
                    )
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_resolve_warns_when_two_agents_are_bound_to_one_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Guards #1035 by proving resolve invokes the binding warning.

    This closes the coverage gap left by #1022, so deleting the call site in
    binding.py makes this test fail.

    Two agents on one channel are unreachable in the curie schema (slack_channel
    is unique), so the rows are seeded into a throwaway schema this test creates
    and drops. No curie object is touched and the production invariant #959
    protects stays in place for everyone else on the box.
    """

    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            # Reap any schema an earlier run leaked: the drop below is a
            # `finally`, which a SIGKILL or a hung query skips. Every name
            # carries the epoch seconds it was created at, and only schemas
            # older than an hour are dropped. That age gate is what makes the
            # sweep safe against a second pytest process running this file
            # concurrently on this shared developer database: a live run's
            # schema is always younger than the threshold, so it can never be
            # reaped mid-flight. A name whose epoch does not parse is left
            # alone rather than guessed at.
            #
            # The sweep is best-effort only where it has to be: a role that
            # cannot see or drop another run's litter gives up quietly, and it
            # runs before the CREATE privilege probe below, so an unguarded
            # privilege failure here would pre-empt that probe's clean skip.
            # Anything else is a bug in the sweep itself and is raised.
            cutoff = int(time.time()) - 3600
            try:
                async with engine.begin() as conn:
                    leaked = await conn.execute(
                        text(
                            "SELECT nspname FROM pg_namespace "
                            "WHERE nspname LIKE 'curie\\_shadow\\_%'"
                        )
                    )
                    for row in leaked.scalars().all():
                        stale = str(row)
                        parts = stale.split("_")
                        if len(parts) != 4 or not parts[2].isdigit():
                            continue
                        if int(parts[2]) >= cutoff:
                            continue
                        # The name comes back from pg_namespace, not from this
                        # process, so it is quoted as an identifier by Postgres
                        # itself rather than pasted into the statement raw.
                        # IF EXISTS because the SELECT above is a check-then-act:
                        # a second reaper can drop the same stranded schema in
                        # between.
                        # CAST(:name AS text) because asyncpg cannot infer a
                        # type for a bare bind parameter in this position and
                        # raises IndeterminateDatatypeError.
                        drop_sql: str = await conn.scalar(
                            text(
                                "SELECT format("
                                "'DROP SCHEMA IF EXISTS %I CASCADE', "
                                "CAST(:name AS text))"
                            ),
                            {"name": stale},
                        )
                        # exec_driver_sql, not execute(text(...)): drop_sql is
                        # already a complete statement with the identifier
                        # quoted by Postgres itself, so it must not be
                        # re-parsed for :name-style bind parameters.
                        await conn.exec_driver_sql(drop_sql)
            except ProgrammingError as exc:
                # Narrow on purpose: a blanket catch here previously turned a
                # broken sweep into a silent no-op, because a plain programming
                # error in the statement above was swallowed and the sweep
                # looked like it had succeeded while reaping nothing. Only
                # sqlstate 42501 (insufficient_privilege) is a legitimate reason
                # for a healthy sweep to give up: this role may lack permission
                # to see or drop another role's schema. `getattr` is defensive:
                # a driver error shaped differently to asyncpg's has no sqlstate
                # and so propagates rather than crashing this check.
                if getattr(getattr(exc, "orig", None), "sqlstate", None) != "42501":
                    raise

            tmp_schema = f"curie_shadow_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f"CREATE SCHEMA {tmp_schema}"))
            except ProgrammingError as exc:
                # Postgres is reachable but this role lacks CREATE on the
                # database. Scoped to the CREATE alone, and within it to
                # sqlstate 42501 (insufficient_privilege) alone, so a syntax
                # error or a missing curie table still fails loudly instead of
                # turning into a green skip. `getattr` is defensive: a driver
                # error shaped differently to asyncpg's has no sqlstate and so
                # propagates rather than crashing this check.
                if getattr(getattr(exc, "orig", None), "sqlstate", None) != "42501":
                    raise
                pytest.skip(f"cannot create a schema as this role: {exc}")

            # The schema exists from here, so its teardown is registered before
            # any later step can fail and leak it.
            try:
                async with engine.begin() as conn:
                    # LIKE ... INCLUDING DEFAULTS copies columns, types, NOT NULL
                    # and defaults, and deliberately NOT indexes, unique
                    # constraints or foreign keys. That is the point: without the
                    # slack_channel unique two agents can share a channel here,
                    # and without the FKs the seeded rows need no parent rows in
                    # curie. INCLUDING ALL or INCLUDING INDEXES would copy the
                    # unique constraint back and silently break this test.
                    await conn.execute(
                        text(
                            f"CREATE TABLE {tmp_schema}.agents "
                            f"(LIKE {_SCHEMA}.agents INCLUDING DEFAULTS)"
                        )
                    )
                    await conn.execute(
                        text(
                            f"CREATE TABLE {tmp_schema}.agent_versions "
                            f"(LIKE {_SCHEMA}.agent_versions INCLUDING DEFAULTS)"
                        )
                    )
                    await conn.execute(
                        text(
                            f"CREATE TABLE {tmp_schema}.deployments "
                            f"(LIKE {_SCHEMA}.deployments INCLUDING DEFAULTS)"
                        )
                    )
                    # The copy carries the `environment` column's reference to
                    # the curie.environment enum, so a schema leaked by a killed
                    # process would keep a live dependency on a curie object and
                    # block 0001_initial's downgrade from dropping that enum.
                    # Downgrading the column to text severs it, leaving a leak as
                    # inert clutter. The resolver only does `d.environment =
                    # 'prod'`, which behaves identically against text.
                    await conn.execute(
                        text(
                            f"ALTER TABLE {tmp_schema}.deployments "
                            "ALTER COLUMN environment TYPE text"
                        )
                    )

                token = uuid.uuid4().hex[:8]
                channel = f"C-{token}"
                agent_a = await _seed_agent(
                    engine,
                    channel=channel,
                    name=f"agent-a-{token}",
                    max_usd=None,
                    max_tokens=None,
                    schema=tmp_schema,
                )
                agent_b = await _seed_agent(
                    engine,
                    channel=channel,
                    name=f"agent-b-{token}",
                    max_usd=None,
                    max_tokens=None,
                    schema=tmp_schema,
                )
                await _seed_deployment(
                    engine,
                    agent_id=agent_a,
                    environment="prod",
                    bundle_ref=f"bundles/a-{token}.zip",
                    schema=tmp_schema,
                    environment_type="text",
                )
                await _seed_deployment(
                    engine,
                    agent_id=agent_b,
                    environment="prod",
                    bundle_ref=f"bundles/b-{token}.zip",
                    schema=tmp_schema,
                    environment_type="text",
                )

                resolver = BindingResolver(
                    engine, WorkerConfig(db_schema=tmp_schema)
                )
                with caplog.at_level(
                    "WARNING", logger="curie_worker.binding"
                ):
                    resolved = await resolver.resolve(channel)

                assert resolved is not None
                # Behavioral only: a WARNING from the binding logger naming both
                # the chosen and the shadowed agent. No prose is matched, so a
                # reword of the message cannot break this, while deleting the
                # call site still does.
                assert any(
                    record.name == "curie_worker.binding"
                    and record.levelno == logging.WARNING
                    and str(agent_a) in record.message
                    and str(agent_b) in record.message
                    for record in caplog.records
                ), caplog.records
            finally:
                async with engine.begin() as conn:
                    # IF EXISTS so this cleanup can never raise 3F000 "schema
                    # does not exist" from inside the `finally` and mask the real
                    # failure that brought us here.
                    await conn.execute(
                        text(f"DROP SCHEMA IF EXISTS {tmp_schema} CASCADE")
                    )
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_warn_if_multiple_agents_bound_names_the_shadowed_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The defense-in-depth branch's logic, covered with no database (#959).

    Unreachable while the unique constraint holds, which is exactly why it is
    exercised against synthetic rows rather than by removing the constraint from a
    shared database. The other half, that `resolve` actually calls this helper, is
    covered by `test_resolve_warns_when_two_agents_are_bound_to_one_channel`: that
    one needs a database, but seeds a throwaway copied schema and leaves the curie
    schema and its constraints untouched.
    """

    chosen = uuid.uuid4()
    shadowed = uuid.uuid4()
    rows = [{"agent_id": chosen}, {"agent_id": shadowed}]

    with caplog.at_level("WARNING"):
        warn_if_multiple_agents_bound("C-shadow", rows)

    assert any(
        "agents bound" in r.message
        and str(shadowed) in r.message
        and str(chosen) in r.message
        for r in caplog.records
    ), caplog.records


def test_warn_if_multiple_agents_bound_is_quiet_for_one_agent() -> None:
    """Negative control: one agent with two active deployments is two rows but one
    agent, so it must NOT warn -- counting rows instead of distinct agents would
    fire on every prod+dev pair."""

    agent_id = uuid.uuid4()
    rows = [{"agent_id": agent_id}, {"agent_id": agent_id}]

    logger = logging.getLogger("curie_worker.binding")
    with unittest.mock.patch.object(logger, "warning") as warned:
        warn_if_multiple_agents_bound("C-single", rows)
    warned.assert_not_called()


def test_deployment_pointing_at_another_agents_version_does_not_resolve() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_a = await _seed_agent(
                engine, channel=channel, name=f"a-{token}", max_usd=None, max_tokens=None
            )
            agent_b = await _seed_agent(
                engine, channel=f"C-other-{token}", name=f"b-{token}", max_usd=None, max_tokens=None
            )
            # Give B a version, then point an active deployment for A at B's version.
            b_version = uuid.uuid4()
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"INSERT INTO {_SCHEMA}.agent_versions "
                        "(id, agent_id, version_label, bundle_ref, created_by) "
                        "VALUES (:id, :agent_id, 'v', 'bundles/b.zip', 't')"
                    ),
                    {"id": b_version, "agent_id": agent_b},
                )
                await conn.execute(
                    text(
                        f"INSERT INTO {_SCHEMA}.deployments "
                        "(id, agent_id, version_id, environment, status) VALUES "
                        f"(:id, :agent_id, :vid, CAST('prod' AS {_SCHEMA}.environment), 'active')"
                    ),
                    {"id": uuid.uuid4(), "agent_id": agent_a, "vid": b_version},
                )

            # The agent-scoped join refuses B's bundle for A's channel.
            resolved = await _resolver(engine).resolve(channel)
            assert resolved is None

            await _cleanup(engine, [agent_a, agent_b])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_behavior_packs_round_trip_and_parse() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            packs_json = (
                '{"greeting": {"enabled": true, "phrases": ["hi"], "reply": "yo"}, '
                '"load": {"enabled": true, "lines": ["Working..."]}}'
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE {_SCHEMA}.agents SET behavior_packs = CAST(:p AS jsonb) "
                        "WHERE id = :id"
                    ),
                    {"p": packs_json, "id": agent_id},
                )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref="bundles/x.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            packs = _resolver(engine).packs_for(resolved)
            assert packs.greeting.enabled is True
            assert packs.greeting.reply == "yo"

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_no_packs_parses_to_all_off_default() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="dev", bundle_ref="bundles/x.zip"
            )
            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.behavior_packs is None
            packs = _resolver(engine).packs_for(resolved)
            assert packs.greeting.enabled is False
            assert packs.tips.enabled is False

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_unknown_channel_resolves_to_none() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")
            resolved = await _resolver(engine).resolve(f"C-nonexistent-{uuid.uuid4().hex}")
            assert resolved is None
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_resolves_approval_required_tools_into_boot_env() -> None:
    # Permission gates (#245): a gated agent's tool list survives the SQL
    # resolve (JSONB decode included) and lands comma-joined in the boot env.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine,
                channel=channel,
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
                approval_tools=["Bash", "mcp__github__create_issue"],
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"bundles/{token}.zip"
            )
            try:
                resolved = await _resolver(engine).resolve(channel)
                assert resolved is not None
                assert resolved.approval_required_tools == [
                    "Bash",
                    "mcp__github__create_issue",
                ]
                env = _resolver(engine).boot_env(resolved, "thread-1")
                assert env[APPROVAL_REQUIRED_ENV] == "Bash,mcp__github__create_issue"
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_boot_env_forwards_scoped_state_tokens_not_the_raw_key() -> None:
    # #410 whole-fix regression guard: the memory and history tokens injected
    # into the sandbox must be scoped "state" tokens (agent-bound, expiring),
    # NEVER the raw shared platform key. A scoped token verifies for THIS agent
    # and only this agent.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"bundles/{token}.zip"
            )
            try:
                resolved = await _resolver(engine).resolve(channel)
                assert resolved is not None
                env = _resolver(engine).boot_env(resolved, "thread-1")

                # The resolver was built with WorkerConfig(db_schema=_SCHEMA),
                # whose api_key default is the platform signing key.
                api_key = WorkerConfig(db_schema=_SCHEMA).api_key
                assert env[MEMORY_TOKEN_ENV] != api_key
                assert env[HISTORY_TOKEN_ENV] != api_key

                # Both tokens verify as scoped "state" credentials for THIS agent.
                assert verify(
                    env[MEMORY_TOKEN_ENV],
                    api_key,
                    agent=str(resolved.agent_id),
                    scope="state",
                )
                assert verify(
                    env[HISTORY_TOKEN_ENV],
                    api_key,
                    agent=str(resolved.agent_id),
                    scope="state",
                )

                # A different agent's identity does not verify against them.
                assert not verify(
                    env[MEMORY_TOKEN_ENV],
                    api_key,
                    agent=str(uuid.uuid4()),
                    scope="state",
                )
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_boot_env_mints_no_token_when_the_signing_key_is_empty() -> None:
    # Fake/local parity: with no platform key configured there is nothing to
    # sign with, so no state token is minted (matching the pre-#410 behavior of
    # forwarding no token). Shown without a DB by constructing the resolved
    # deployment directly -- boot_env is pure and never touches the engine.
    engine = create_async_engine(_DB_URL)
    try:
        resolver = BindingResolver(engine, WorkerConfig(db_schema=_SCHEMA, api_key=""))
        resolved = ResolvedDeployment(
            agent_id=uuid.uuid4(),
            agent_name="test-agent",
            version_id=uuid.uuid4(),
            version_label="v",
            bundle_ref=None,
            max_usd_per_day=None,
            max_output_tokens_per_run=None,
        )
        env = resolver.boot_env(resolved, "thread-1")
        assert MEMORY_TOKEN_ENV not in env
        assert HISTORY_TOKEN_ENV not in env
    finally:
        asyncio.run(engine.dispose())


def test_resolves_approval_routes_from_the_agent_row() -> None:
    # Route bindings (#247): the per-agent JSONB map survives the SQL resolve
    # (decode included) so the kernel can route approval cards through it.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine,
                channel=channel,
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
                approval_routes={"managers": {"channel": "C_MGRS"}},
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"b/{token}.zip"
            )
            try:
                resolved = await _resolver(engine).resolve(channel)
                assert resolved is not None
                assert resolved.approval_routes == {"managers": {"channel": "C_MGRS"}}
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_resolves_connector_secrets_into_boot_env() -> None:
    # Connector secrets (#429): the per-agent JSONB name->value map survives the
    # SQL resolve (decode included) and is injected by name into the sandbox boot
    # env so an authed-MCP bundle can read its token.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine,
                channel=channel,
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
                secrets={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_seeded"},
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"b/{token}.zip"
            )
            try:
                resolver = _resolver(engine)
                resolved = await resolver.resolve(channel)
                assert resolved is not None
                assert resolved.secrets == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_seeded"}
                env = resolver.boot_env(resolved, thread_key="t1")
                assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_seeded"
                # secrets_for resolves the same map by agent_id (the eval lane).
                by_id = await resolver.secrets_for(agent_id)
                assert by_id == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_seeded"}
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_reads_thinking_for_eval_boots_by_agent_id() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            agent_id = await _seed_agent(
                engine,
                channel=f"C-{token}",
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
            )
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(f"UPDATE {_SCHEMA}.agents SET thinking = :thinking WHERE id = :id"),
                        {"thinking": "high", "id": agent_id},
                    )

                resolver = _resolver(engine)
                assert await resolver.thinking_for(agent_id) == "high"

                async with engine.begin() as conn:
                    await conn.execute(
                        text(f"UPDATE {_SCHEMA}.agents SET thinking = NULL WHERE id = :id"),
                        {"id": agent_id},
                    )

                assert await resolver.thinking_for(agent_id) is None
                assert await resolver.thinking_for(uuid.uuid4()) is None
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_reserved_connector_secret_is_dropped_order_independently() -> None:
    # #457 order-independence regression. A connector secret named
    # ANTHROPIC_BASE_URL redirects the model credential. The old `if name not in
    # env` guard did NOT stop it on the DEFAULT path (model_base_url unset),
    # because apply_model_env sets ANTHROPIC_BASE_URL AFTER the injection loop,
    # so the reserved name was absent from env at loop time and survived. The
    # fix must filter by the reserved SET, not by env state. This test MUST run
    # the default path (_resolver builds WorkerConfig with model_base_url unset)
    # or the bug does not reproduce.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine,
                channel=channel,
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
                secrets={
                    "ANTHROPIC_BASE_URL": "http://evil",
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_ok",
                },
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"b/{token}.zip"
            )
            try:
                resolver = _resolver(engine)
                resolved = await resolver.resolve(channel)
                assert resolved is not None
                env = resolver.boot_env(resolved, thread_key="t1")

                # The injected reserved value must never land in the boot env.
                assert env.get("ANTHROPIC_BASE_URL") != "http://evil"
                # Negative control: the legitimate connector secret is delivered.
                assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_ok"
                # The dropped reserved key is excluded from the marker; only the
                # legitimately injected key is listed.
                keys = env[CONNECTOR_SECRET_KEYS_ENV].split(",")
                assert keys == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
                assert "ANTHROPIC_BASE_URL" not in keys
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())
