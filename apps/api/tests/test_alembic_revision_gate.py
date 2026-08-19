import subprocess
import sys
from pathlib import Path

import yaml
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "check-alembic-revisions.py"
ALEMBIC_TREE = REPO_ROOT / "apps" / "api" / "alembic"
CHECK_COMMAND = "uv run python scripts/check-alembic-revisions.py"


def _write_revision(
    script_location: Path,
    filename: str,
    revision: str,
    down_revision: str | tuple[str, ...] | None,
) -> None:
    versions = script_location / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / filename).write_text(
        "\n".join(
            [
                f"revision = {revision!r}",
                f"down_revision = {down_revision!r}",
                "branch_labels = None",
                "depends_on = None",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_gate(script_location: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CHECKER)]
    if script_location is not None:
        command.extend(["--script-location", str(script_location)])
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_real_migration_tree_has_one_reported_head() -> None:
    expected_head = ScriptDirectory(str(ALEMBIC_TREE)).get_current_head()

    result = _run_gate()

    assert expected_head is not None
    assert result.returncode == 0, result.stderr
    assert expected_head in result.stdout


def test_duplicate_numeric_filename_prefix_fails_before_graph_validation(
    tmp_path: Path,
) -> None:
    _write_revision(tmp_path, "0001_base.py", "base", None)
    _write_revision(tmp_path, "0001_collision.py", "different_revision", "base")

    result = _run_gate(tmp_path)

    assert result.returncode == 1, result.stderr
    assert "duplicate numeric revision" in result.stderr.lower()
    assert "0001" in result.stderr
    assert "0001_base.py" in result.stderr
    assert "0001_collision.py" in result.stderr


def test_unique_numbered_sibling_revisions_fail_with_both_heads(tmp_path: Path) -> None:
    _write_revision(tmp_path, "0001_base.py", "base", None)
    _write_revision(tmp_path, "0002_left.py", "left_branch", "base")
    _write_revision(tmp_path, "0003_right.py", "right_branch", "base")

    result = _run_gate(tmp_path)

    assert result.returncode == 1, result.stderr
    assert "expected exactly one alembic head" in result.stderr.lower()
    assert "left_branch" in result.stderr
    assert "right_branch" in result.stderr


def test_merge_revision_restores_one_head(tmp_path: Path) -> None:
    _write_revision(tmp_path, "0001_base.py", "base", None)
    _write_revision(tmp_path, "0002_left.py", "left_branch", "base")
    _write_revision(tmp_path, "0003_right.py", "right_branch", "base")
    _write_revision(
        tmp_path,
        "0004_merge.py",
        "merged_head",
        ("left_branch", "right_branch"),
    )

    result = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "merged_head" in result.stdout


def test_python_ci_job_runs_exact_gate_before_dev_stack() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text())
    steps = workflow["jobs"]["python"]["steps"]

    matching_steps = [step for step in steps if step.get("run") == CHECK_COMMAND]
    assert len(matching_steps) == 1
    assert matching_steps[0]["name"] == "Alembic revision gate"

    gate_index = steps.index(matching_steps[0])
    stack_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Start dev stack"
    )
    assert gate_index < stack_index
