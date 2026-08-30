"""Verify that the candidate migrations upgrade the latest released database.

The gate walks six phases per pair:

    released upgrade -> released head check -> SEED
        -> candidate upgrade -> candidate head check -> READ-BACK

The four alembic phases are #1706's original contract: they prove the candidate
migrations RUN against a database stamped by the released line. The two phases
added by #2098 prove the RESULT IS READABLE, which is a different claim and the
one #1914 needed: migration 0021 backfilled `curie.agent_channels` verbatim out
of `agents.slack_channel`, the copied rows then failed `ChannelBinding`'s
address rule, and `GET /agents` returned 500 for every agent while every
migration in the chain had reported success.

SEED writes rows in the shapes the RELEASED code actually permitted, straight
into the scratch database over SQL. It deliberately does not go through a
current-HEAD model: constructing rows that satisfy today's validators would
prove only that today's validators accept what they just produced. READ-BACK
then serializes the migrated rows through the CANDIDATE tree's `AgentOut` and
requires that they load.
"""

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.dev.yaml"
STABLE_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]+$")
# Always HEAD's copy of the runner, never the tree being judged. See that file's
# docstring: it is one fixed assertion harness executed inside the candidate's
# environment, so the thing that varies across directions is the package it
# imports, not the assertions themselves.
READBACK_SCRIPT = REPO_ROOT / "scripts" / "released_upgrade_readback.py"


@dataclass(frozen=True)
class SelfTestDirection:
    """One pinned `--self-test` pair and the outcome it must produce.

    `expect_failure_phase` is the phase the pair MUST fail in, or `None` for a
    direction that must pass cleanly. `markers` are substrings the failure output
    must contain -- they are the defense against the right phase failing for the
    wrong reason, which is not hypothetical: a `MissingGreenlet` in the read-back
    also lands in the `read-back` phase, and only the markers separate "the
    failure we pinned" from "some failure".
    """

    released_ref: str
    candidate_ref: str
    expect_failure_phase: str | None
    markers: tuple[str, ...]


# The pinned directions. #1706's negative control is the FIRST row and is
# retained verbatim, markers included -- a self test that only knows how to fail
# is inert, and one that has lost its must-fail direction is worse. The other two
# rows are #2098's addition, and they are what keep both seed branches alive:
# every direction seeds through the legacy `agents.slack_channel` column (v0.6.2
# is pre-0021), while the bare gate is `v0.8.0 -> HEAD` and seeds through
# `agent_channels`.
SELF_TEST_DIRECTIONS: tuple[SelfTestDirection, ...] = (
    SelfTestDirection(
        released_ref="v0.6.2",
        candidate_ref="v0.7.0-rc.1",
        expect_failure_phase="candidate upgrade",
        markers=(
            "asyncpg.exceptions.UndefinedTableError",
            'relation "curie.agent_channels" does not exist',
            "0022_approvals_reply_kind.py",
        ),
    ),
    SelfTestDirection(
        # #1914's reproduction. v0.6.2 upgrades to v0.7.3 CLEANLY today, so the
        # only thing that can redden this direction is the read-back phase --
        # which makes it a clean pin on the read path rather than on migrations.
        released_ref="v0.6.2",
        candidate_ref="v0.7.3",
        expect_failure_phase="read-back",
        markers=("C-0a1b2c3d", "is not a Slack channel ID"),
    ),
    SelfTestDirection(
        # The must-PASS direction: v0.8.0 is the first tag containing the
        # tolerant `ChannelBindingOut`, so the same legacy rows that break
        # v0.7.3 must serialize here.
        released_ref="v0.6.2",
        candidate_ref="v0.8.0",
        expect_failure_phase=None,
        markers=(),
    ),
)


@dataclass(frozen=True)
class SeedAgent:
    """One row the seed writes before the candidate upgrade.

    `legacy` means the address is a shape the RELEASED code stored but the
    CURRENT write validator rejects. That flag is asserted against the live
    `curie_api.schemas._validate_channel_binding` in the gate's tests, so an
    address softened to make the gate green stops being legacy and reddens the
    suite instead.

    `approval_route_address` seeds `agents.approval_routes` with a route
    resolving to the same rejected address. Without it the sibling read
    projections (`ApprovalTargetOut`, `ApprovalApproversOut`,
    `ApprovalRouteBindingOut`) -- made tolerant by the SAME fix commit as
    `ChannelBindingOut` -- could be reverted with the gate staying green.
    """

    name: str
    address: str
    kind: str
    legacy: bool
    approval_route_address: str | None


# Three agents, one binding each. The names are prefixed `gate-` so they are
# unmistakably synthetic, and all three names and all three addresses are
# distinct so neither the seed nor 0021's backfill can trip
# `agents_slack_channel_key` / `agent_channels_kind_address_key`.
#
# `C0EXAMPLE1` is the repo's sanctioned placeholder Slack id (`.gitleaks.toml`
# stopword); `#general` and `C-0a1b2c3d` cannot match the scanner's
# conversation-id rule at all.
SEED_FIXTURE: tuple[SeedAgent, ...] = (
    SeedAgent(
        # #143's shape: a literal channel NAME, stored before the validator
        # existed and reported as a success at the time.
        name="gate-legacy-hash",
        address="#general",
        kind="slack",
        legacy=True,
        approval_route_address=None,
    ),
    SeedAgent(
        # #1914's shape, exactly: a `C-<hex>` address.
        name="gate-legacy-prefix",
        address="C-0a1b2c3d",
        kind="slack",
        legacy=True,
        approval_route_address="C-0a1b2c3d",
    ),
    SeedAgent(
        # The valid neighbour. It is what proves the "one bad row takes the
        # whole endpoint down" property: a tolerant read path must return
        # EVERY agent, not merely the ones that happen to be well-formed.
        name="gate-valid",
        address="C0EXAMPLE1",
        kind="slack",
        legacy=False,
        approval_route_address=None,
    ),
)


@dataclass(frozen=True)
class ColumnInfo:
    """One `information_schema.columns` row from the scratch database.

    `is_nullable` keeps Postgres' verbatim `"YES"` / `"NO"` rather than being
    coerced to a bool, so a mis-parsed introspection row cannot quietly read as
    "nullable" and disarm the mandatory-column check below.
    """

    table_name: str
    column_name: str
    is_nullable: str
    column_default: str | None


class GateError(RuntimeError):
    """A released upgrade gate setup or cleanup failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True)
class PairResult:
    phase: str
    returncode: int
    output: str


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise GateError(f"could not run {command[0]}: {exc}") from exc
    return CommandResult(completed.returncode, completed.stdout or "")


def _checked_output(
    command: list[str],
    *,
    description: str,
    cwd: Path = REPO_ROOT,
) -> str:
    result = _run(command, cwd=cwd)
    if result.returncode != 0:
        if result.output:
            print(result.output, end="" if result.output.endswith("\n") else "\n")
        raise GateError(f"{description} failed with exit code {result.returncode}")
    return result.output


def _resolve_ref(ref: str) -> str:
    commit = _checked_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        description=f"resolving Git ref {ref}",
    ).strip()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise GateError(f"Git ref {ref} resolved to an invalid commit: {commit!r}")
    return commit


def _latest_released_tag() -> tuple[str, str]:
    main_commit = _resolve_ref("origin/main")
    tag_output = _checked_output(
        ["git", "tag", "--merged", main_commit, "--list"],
        description="listing stable tags reachable from origin/main",
    )
    stable_tags: list[tuple[tuple[int, int, int], str]] = []
    for tag in tag_output.splitlines():
        match = STABLE_TAG_PATTERN.fullmatch(tag)
        if match is None:
            continue
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        stable_tags.append((version, tag))
    if not stable_tags:
        raise GateError("no stable vX.Y.Z release tag is reachable from origin/main")
    _, latest_tag = max(stable_tags)
    return latest_tag, _resolve_ref(latest_tag)


def _compose_command(*arguments: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *arguments]


def _check_postgres() -> None:
    _checked_output(
        ["docker", "compose", "version"],
        description="checking Docker Compose availability",
    )
    _checked_output(
        _compose_command(
            "exec",
            "-T",
            "postgres",
            "pg_isready",
            "-U",
            "postgres",
            "-d",
            "postgres",
        ),
        description="checking the compose Postgres service",
    )


def _database_command(sql: str, *, database_name: str = "postgres") -> list[str]:
    """psql into `database_name`, defaulting to the maintenance database.

    The default keeps every pre-#2098 caller (`_reset_database`, `_cleanup`)
    byte-for-byte identical: DROP/CREATE DATABASE cannot be issued from inside
    the database being dropped. The seed is the only caller that needs the
    SCRATCH database, and it is the reason this parameter exists.
    """

    return _compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        database_name,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )


def _scratch_query(database_name: str, sql: str) -> str:
    """Run a read-only query against the scratch database and return stdout.

    `-t -A` strips the header, the row-count footer and the column padding, so
    the caller parses `|`-separated fields instead of psql's table art.
    """

    return _checked_output(
        _compose_command(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            database_name,
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ),
        description=f"querying scratch database {database_name}",
    )


def _reset_database(database_name: str) -> None:
    _checked_output(
        _database_command(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'),
        description=f"dropping stale scratch database {database_name}",
    )
    _checked_output(
        _database_command(f'CREATE DATABASE "{database_name}"'),
        description=f"creating scratch database {database_name}",
    )


def _add_worktree(path: Path, commit: str, *, label: str) -> None:
    _checked_output(
        ["git", "worktree", "add", "--detach", str(path), commit],
        description=f"creating detached {label} worktree",
    )


def _alembic_command(tree: Path, *arguments: str) -> list[str]:
    return [
        "uv",
        "run",
        "--frozen",
        "--project",
        str(tree),
        "--directory",
        str(tree / "apps" / "api"),
        "alembic",
        *arguments,
    ]


def _run_alembic(
    tree: Path,
    *,
    database_url: str,
    phase: str,
    ref: str,
    commit: str,
    arguments: tuple[str, ...],
) -> CommandResult:
    print(f"=== {phase}: {ref} ({commit}) ===", flush=True)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    try:
        completed = subprocess.run(
            _alembic_command(tree, *arguments),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise GateError(f"could not run {phase}: {exc}") from exc
    output = completed.stdout or ""
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return CommandResult(completed.returncode, output)


def _readback_command(tree: Path, *arguments: str) -> list[str]:
    """Run HEAD's read-back runner inside `tree`'s environment.

    The `uv run --frozen --project <tree> --directory <tree>/apps/api` prefix is
    `_alembic_command`'s, verbatim, because it is the same trick: resolve the
    executable's dependencies from the tree under judgement. The inversion here
    is that the SCRIPT is HEAD's (`READBACK_SCRIPT`, never `tree`'s copy) while
    the `curie_api` it imports is the tree's -- one fixed assertion harness
    pointed at whichever read models we are judging.
    """

    return [
        "uv",
        "run",
        "--frozen",
        "--project",
        str(tree),
        "--directory",
        str(tree / "apps" / "api"),
        "python",
        str(READBACK_SCRIPT),
        *arguments,
    ]


def _run_readback(
    tree: Path,
    *,
    database_url: str,
    phase: str,
    ref: str,
    commit: str,
) -> CommandResult:
    """Follow `_run_alembic`'s contract exactly: banner, env, combined output.

    The output has to come back combined and echoed for the same reason the
    alembic phases do -- `_run_self_test` pins markers against it, and a marker
    that landed on a stderr stream the caller never captured is a marker that
    silently stops defending anything.

    Only `DATABASE_URL` is added to the environment. `curie_api` was verified to
    import with nothing else set at v0.7.3, v0.8.0 and HEAD; a candidate that
    needs more would fail here loudly rather than skip the phase.
    """

    arguments: list[str] = []
    for agent in SEED_FIXTURE:
        arguments.extend(("--expect-agent", agent.name))
    for agent in SEED_FIXTURE:
        arguments.extend(("--expect-address", agent.address))

    print(f"=== {phase}: {ref} ({commit}) ===", flush=True)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    try:
        completed = subprocess.run(
            _readback_command(tree, *arguments),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise GateError(f"could not run {phase}: {exc}") from exc
    output = completed.stdout or ""
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return CommandResult(completed.returncode, output)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _json_literal(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return "NULL"
    return f"{_sql_literal(json.dumps(payload))}::jsonb"


def _introspect_columns(database_name: str) -> tuple[ColumnInfo, ...]:
    """Read the scratch database's `curie` schema after the released upgrade.

    The seed cannot be written against one hard-coded schema: the released ref is
    `v0.6.2` for the `--self-test` directions (pre-0021, `agents.slack_channel`)
    and the latest stable tag for the bare gate (post-0021, `agent_channels`).
    Introspecting is what lets one fixture serve both.
    """

    rows = _scratch_query(
        database_name,
        "SELECT table_name, column_name, is_nullable, "
        "coalesce(column_default, '') FROM information_schema.columns "
        "WHERE table_schema = 'curie' ORDER BY table_name, ordinal_position",
    )
    columns: list[ColumnInfo] = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        # maxsplit=3: a column DEFAULT expression may itself contain a `|`, and
        # it is the last field, so everything after the third separator is it.
        fields = line.split("|", 3)
        if len(fields) != 4:
            raise GateError(f"could not parse an information_schema row: {line!r}")
        table_name, column_name, is_nullable, column_default = fields
        columns.append(
            ColumnInfo(
                table_name=table_name,
                column_name=column_name,
                is_nullable=is_nullable,
                # psql renders SQL NULL as an empty field under `-t -A`, and a
                # real default is never the empty string, so the round-trip is
                # unambiguous.
                column_default=column_default or None,
            )
        )
    return tuple(columns)


def _verify_mandatory_columns(
    columns: tuple[ColumnInfo, ...],
    *,
    filled: dict[str, set[str]],
) -> None:
    """Refuse to seed if a mandatory column the fixture does not fill exists.

    Only the tables this branch actually writes are checked; a table the branch
    ignores has no rows to be incomplete.

    The value here is the message, not the rejection: without it, the next person
    to add a NOT NULL column without a server default gets a bare Postgres
    not-null error from inside a CI gate they did not write. With it, they are
    told which column to teach the fixture about.
    """

    unfilled = [
        f"{column.table_name}.{column.column_name}"
        for column in columns
        if column.table_name in filled
        and column.is_nullable == "NO"
        and column.column_default is None
        and column.column_name not in filled[column.table_name]
    ]
    if unfilled:
        raise GateError(
            "the released-upgrade seed fixture does not fill these mandatory "
            f"columns: {', '.join(unfilled)}. Add a value for each to "
            "SEED_FIXTURE's rendered statements in scripts/"
            "check-released-upgrade.py."
        )


def _plan_seed_statements(columns: tuple[ColumnInfo, ...]) -> tuple[str, ...]:
    """Render the SQL that writes `SEED_FIXTURE` into the released schema.

    Branches on `agents.slack_channel` -- the pre/post-0021 discriminator -- and
    that same discriminator selects the `approval_routes` shape, because the 0021
    era and the 0034 era coincide at both refs we pin. That coupling is an
    assumption rather than a law, and it is made safe by the read-back: a future
    released ref landing between the eras makes the seeded route go missing from
    the dump, and the read-back FAILS naming the address instead of passing
    vacuously.

    Raises `GateError` rather than returning an empty plan when neither binding
    location exists. A seed that no-ops turns the read-back into a vacuous pass,
    which is the exact class of failure #2098 exists to close.
    """

    present = {(column.table_name, column.column_name) for column in columns}
    statements: list[str] = []

    if ("agents", "slack_channel") in present:
        _verify_mandatory_columns(
            columns,
            filled={"agents": {"id", "name", "slack_channel", "approval_routes"}},
        )
        for agent in SEED_FIXTURE:
            route = _json_literal(
                None
                if agent.approval_route_address is None
                # Pre-0034 storage. Migration 0034 rewrites it into the split
                # `resolution` shape on the way up with NO validation, which is
                # what carries the legacy address forward into the read models.
                else {"deploy": {"channel": agent.approval_route_address}}
            )
            statements.append(
                "INSERT INTO curie.agents (id, name, slack_channel, "
                "approval_routes)\n"
                f"VALUES (gen_random_uuid(), {_sql_literal(agent.name)}, "
                f"{_sql_literal(agent.address)}, {route})"
            )
        return tuple(statements)

    if ("agent_channels", "address") in present:
        _verify_mandatory_columns(
            columns,
            filled={
                "agents": {"id", "name", "approval_routes"},
                "agent_channels": {"id", "agent_id", "kind", "address"},
            },
        )
        for agent in SEED_FIXTURE:
            route = _json_literal(
                None
                if agent.approval_route_address is None
                # Post-0034 storage: already the split shape.
                else {
                    "deploy": {
                        "resolution": {
                            "kind": agent.kind,
                            "address": agent.approval_route_address,
                        }
                    }
                }
            )
            # One statement per agent, so the binding is written in the same
            # transaction as the row it belongs to and the new id never has to be
            # round-tripped back through psql. `endpoint` / `adapter` are left
            # NULL, which is the posture `agent_channels_route_pair_ck` permits
            # and the one 0024 backfills existing rows to.
            statements.append(
                "WITH seeded AS (\n"
                "    INSERT INTO curie.agents (id, name, approval_routes)\n"
                f"    VALUES (gen_random_uuid(), {_sql_literal(agent.name)}, "
                f"{route})\n"
                "    RETURNING id\n"
                ")\n"
                "INSERT INTO curie.agent_channels (id, agent_id, kind, address)\n"
                f"SELECT gen_random_uuid(), seeded.id, "
                f"{_sql_literal(agent.kind)}, {_sql_literal(agent.address)}\n"
                "FROM seeded"
            )
        return tuple(statements)

    raise GateError(
        "the released schema has neither agents.slack_channel nor an "
        "agent_channels.address column, so the seed has nowhere to write a "
        "channel binding; refusing to run a read-back that would pass "
        "vacuously"
    )


def _seed_released_database(database_name: str, *, phase: str) -> None:
    """Write the fixture into the released database, between the two upgrades.

    Every failure in here is a SETUP fault, not a gate verdict: it raises
    `GateError`, which `main` reports as `Released upgrade gate failed:` and
    exit 1, distinct from a `PairResult` verdict. A broken seed must never be
    reported as "the migration is fine" or as "the read path is broken".
    """

    print(f"=== {phase}: {database_name} ===", flush=True)
    columns = _introspect_columns(database_name)
    statements = _plan_seed_statements(columns)
    for statement in statements:
        _checked_output(
            _database_command(statement, database_name=database_name),
            description=f"seeding the released database {database_name}",
        )
    print(
        f"Seeded {len(statements)} released-shaped agent rows into "
        f"{database_name}."
    )


def _upgrade_pair(
    released_tree: Path,
    candidate_tree: Path,
    *,
    database_url: str,
    database_name: str,
    released_ref: str,
    released_commit: str,
    candidate_ref: str,
    candidate_commit: str,
) -> PairResult:
    released_phases = (
        (
            "released upgrade",
            released_tree,
            released_ref,
            released_commit,
            ("upgrade", "head"),
        ),
        (
            "released head check",
            released_tree,
            released_ref,
            released_commit,
            ("current", "--check-heads"),
        ),
    )
    candidate_phases = (
        (
            "candidate upgrade",
            candidate_tree,
            candidate_ref,
            candidate_commit,
            ("upgrade", "head"),
        ),
        (
            "candidate head check",
            candidate_tree,
            candidate_ref,
            candidate_commit,
            ("current", "--check-heads"),
        ),
    )

    for phase, tree, ref, commit, arguments in released_phases:
        result = _run_alembic(
            tree,
            database_url=database_url,
            phase=phase,
            ref=ref,
            commit=commit,
            arguments=arguments,
        )
        if result.returncode != 0:
            return PairResult(phase, result.returncode, result.output)

    # The seed sits HERE, between the two lines' phases, because that is the only
    # window in which the released schema is what an operator's database actually
    # looks like. Seeding earlier has no schema to write into; seeding later
    # writes rows the candidate migrations never had to carry.
    _seed_released_database(database_name, phase="seed")

    for phase, tree, ref, commit, arguments in candidate_phases:
        result = _run_alembic(
            tree,
            database_url=database_url,
            phase=phase,
            ref=ref,
            commit=commit,
            arguments=arguments,
        )
        if result.returncode != 0:
            return PairResult(phase, result.returncode, result.output)

    readback = _run_readback(
        candidate_tree,
        database_url=database_url,
        phase="read-back",
        ref=candidate_ref,
        commit=candidate_commit,
    )
    return PairResult("read-back", readback.returncode, readback.output)


def _cleanup(
    worktrees: list[Path],
    *,
    database_name: str,
    database_touched: bool,
) -> None:
    errors: list[str] = []
    for worktree in reversed(worktrees):
        try:
            result = _run(
                ["git", "worktree", "remove", "--force", str(worktree)]
            )
        except GateError as exc:
            errors.append(str(exc))
        else:
            if result.returncode != 0:
                errors.append(
                    f"removing scratch worktree {worktree} failed: "
                    f"{result.output.strip()}"
                )
    if database_touched:
        try:
            result = _run(
                _database_command(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
            )
        except GateError as exc:
            errors.append(str(exc))
        else:
            if result.returncode != 0:
                errors.append(
                    f"dropping scratch database {database_name} failed: "
                    f"{result.output.strip()}"
                )
    if errors:
        rendered = "\n".join(f"  {error}" for error in errors)
        raise GateError(f"scratch cleanup failed:\n{rendered}")


def _run_pair(
    *,
    released_ref: str,
    released_commit: str,
    candidate_ref: str,
    candidate_commit: str,
    current_candidate: bool,
) -> PairResult:
    _check_postgres()
    database_name = f"curie_upgrade_{os.getpid()}_{secrets.token_hex(4)}"
    database_url = (
        "postgresql+asyncpg://postgres:postgres@localhost:25432/"
        f"{database_name}"
    )
    worktrees: list[Path] = []
    database_touched = False
    with tempfile.TemporaryDirectory(prefix="curie-released-upgrade-") as temp_dir:
        temp_root = Path(temp_dir)
        try:
            database_touched = True
            _reset_database(database_name)

            released_tree = temp_root / "released"
            _add_worktree(released_tree, released_commit, label="released")
            worktrees.append(released_tree)

            if current_candidate:
                candidate_tree = REPO_ROOT
            else:
                candidate_tree = temp_root / "candidate"
                _add_worktree(candidate_tree, candidate_commit, label="candidate")
                worktrees.append(candidate_tree)

            return _upgrade_pair(
                released_tree,
                candidate_tree,
                database_url=database_url,
                database_name=database_name,
                released_ref=released_ref,
                released_commit=released_commit,
                candidate_ref=candidate_ref,
                candidate_commit=candidate_commit,
            )
        finally:
            _cleanup(
                worktrees,
                database_name=database_name,
                database_touched=database_touched,
            )


def _describe(direction: SelfTestDirection) -> str:
    return f"{direction.released_ref} to {direction.candidate_ref}"


def _check_direction(direction: SelfTestDirection) -> str | None:
    """Run one pinned direction. Returns a mismatch description, or None on match.

    It RETURNS the mismatch instead of raising it so `_run_self_test` can walk
    every direction and report all of them. A self test that stops at the first
    mismatch hides the second one behind a fix for the first, and the whole point
    of the table is that the directions defend different properties.
    """

    result = _run_pair(
        released_ref=direction.released_ref,
        released_commit=_resolve_ref(direction.released_ref),
        candidate_ref=direction.candidate_ref,
        candidate_commit=_resolve_ref(direction.candidate_ref),
        current_candidate=False,
    )

    if direction.expect_failure_phase is None:
        if result.returncode != 0:
            return (
                f"self test direction {_describe(direction)} was expected to "
                f"upgrade and read back cleanly, but failed during "
                f"{result.phase} with exit code {result.returncode}"
            )
        return None

    if result.returncode == 0:
        return (
            f"self test direction {_describe(direction)} unexpectedly "
            f"succeeded; it must fail during {direction.expect_failure_phase}"
        )
    if result.phase != direction.expect_failure_phase:
        return (
            f"self test direction {_describe(direction)} failed during "
            f"{result.phase}, before the expected "
            f"{direction.expect_failure_phase} failure"
        )
    missing_markers = [
        marker for marker in direction.markers if marker not in result.output
    ]
    if missing_markers:
        # The markers are what separate "the failure we pinned" from "some
        # failure in the right phase". Dropping this check would let a
        # MissingGreenlet, or any incidental error, stand in for the defect the
        # direction exists to reproduce.
        return (
            f"self test direction {_describe(direction)} failed during "
            f"{result.phase} but did not report the known markers: "
            f"{', '.join(missing_markers)}"
        )
    return None


def _run_self_test() -> int:
    mismatches: list[str] = []
    for direction in SELF_TEST_DIRECTIONS:
        expectation = (
            "must pass"
            if direction.expect_failure_phase is None
            else f"must fail during {direction.expect_failure_phase}"
        )
        print(
            f"=== self test direction {_describe(direction)} ({expectation}) ===",
            flush=True,
        )
        mismatch = _check_direction(direction)
        if mismatch is not None:
            mismatches.append(mismatch)

    if mismatches:
        rendered = "\n".join(f"  {mismatch}" for mismatch in mismatches)
        raise GateError(f"released upgrade self test mismatched:\n{rendered}")

    print(
        f"Released upgrade self test passed across {len(SELF_TEST_DIRECTIONS)} "
        "pinned directions."
    )
    return 0


def _run_requested_pair(released_ref: str, candidate_ref: str) -> int:
    released_commit = _resolve_ref(released_ref)
    candidate_commit = _resolve_ref(candidate_ref)
    result = _run_pair(
        released_ref=released_ref,
        released_commit=released_commit,
        candidate_ref=candidate_ref,
        candidate_commit=candidate_commit,
        current_candidate=False,
    )
    if result.returncode != 0:
        print(
            f"Released upgrade gate failed during {result.phase} with exit "
            f"code {result.returncode}.",
            file=sys.stderr,
        )
    return result.returncode


def _run_normal() -> int:
    released_ref, released_commit = _latest_released_tag()
    candidate_commit = _resolve_ref("HEAD")
    print(
        f"Selected latest stable release {released_ref} ({released_commit}) "
        "reachable from origin/main."
    )
    result = _run_pair(
        released_ref=released_ref,
        released_commit=released_commit,
        candidate_ref="HEAD",
        candidate_commit=candidate_commit,
        current_candidate=True,
    )
    if result.returncode != 0:
        print(
            f"Released upgrade gate failed during {result.phase} with exit "
            f"code {result.returncode}.",
            file=sys.stderr,
        )
        return result.returncode
    print(
        f"Released upgrade gate passed from {released_ref} to candidate "
        f"{candidate_commit}."
    )
    return 0


def _raise_interrupted(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upgrade a released Curie database with candidate migrations."
    )
    parser.add_argument(
        "--released-ref",
        help="Released Git ref for an explicit upgrade pair.",
    )
    parser.add_argument(
        "--candidate-ref",
        help="Candidate Git ref for an explicit upgrade pair.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Require every pinned direction: the known revision collision, the "
            "known read-back failure, and the direction that must pass."
        ),
    )
    args = parser.parse_args()

    if args.self_test and (args.released_ref or args.candidate_ref):
        parser.error("--self-test cannot be combined with explicit refs")
    if bool(args.released_ref) != bool(args.candidate_ref):
        parser.error("--released-ref and --candidate-ref must be provided together")

    signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        if args.self_test:
            return _run_self_test()
        if args.released_ref and args.candidate_ref:
            return _run_requested_pair(args.released_ref, args.candidate_ref)
        return _run_normal()
    except KeyboardInterrupt:
        print("Released upgrade gate interrupted after cleanup.", file=sys.stderr)
        return 130
    except GateError as exc:
        print(f"Released upgrade gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
