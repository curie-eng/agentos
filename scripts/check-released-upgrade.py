"""Verify that the candidate migrations upgrade the latest released database."""

import argparse
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

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.dev.yaml"
STABLE_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]+$")
SELF_TEST_RELEASED_REF = "v0.6.2"
SELF_TEST_CANDIDATE_REF = "v0.7.0-rc.1"


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


def _database_command(sql: str) -> list[str]:
    return _compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
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


def _upgrade_pair(
    released_tree: Path,
    candidate_tree: Path,
    *,
    database_url: str,
    released_ref: str,
    released_commit: str,
    candidate_ref: str,
    candidate_commit: str,
) -> PairResult:
    phases = (
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
    for phase, tree, ref, commit, arguments in phases:
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
    return PairResult("candidate head check", 0, "")


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


def _run_self_test() -> int:
    released_commit = _resolve_ref(SELF_TEST_RELEASED_REF)
    candidate_commit = _resolve_ref(SELF_TEST_CANDIDATE_REF)
    result = _run_pair(
        released_ref=SELF_TEST_RELEASED_REF,
        released_commit=released_commit,
        candidate_ref=SELF_TEST_CANDIDATE_REF,
        candidate_commit=candidate_commit,
        current_candidate=False,
    )
    if result.returncode == 0:
        raise GateError("self test pair unexpectedly upgraded successfully")
    if result.phase != "candidate upgrade":
        raise GateError(
            f"self test failed during {result.phase}, before the expected "
            "candidate collision"
        )
    expected_markers = (
        "asyncpg.exceptions.UndefinedTableError",
        'relation "curie.agent_channels" does not exist',
        "0022_approvals_reply_kind.py",
    )
    missing_markers = [
        marker for marker in expected_markers if marker not in result.output
    ]
    if missing_markers:
        raise GateError(
            "self test candidate failure did not report the known revision "
            f"collision markers: {', '.join(missing_markers)}"
        )
    print(
        "Released upgrade self test passed by observing the known candidate "
        "revision collision."
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
        help="Require the known v0.6.2 to v0.7.0-rc.1 collision.",
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
