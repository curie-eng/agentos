"""Regression coverage for pull request secret scan scope."""

from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gitleaks.yaml"


def _workflow() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _gitleaks_job() -> dict[str, Any]:
    job = _workflow()["jobs"]["gitleaks"]
    assert isinstance(job, dict)
    return job


def _gitleaks_image() -> str:
    image = _gitleaks_job()["env"]["GITLEAKS_IMAGE"]
    assert isinstance(image, str)
    assert "@sha256:" in image, "the test must run the immutable workflow image"
    return image


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "--message", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "--initial-branch", "main")
    _git(path, "config", "user.name", "Scope Test")
    _git(path, "config", "user.email", "scope-test@example.invalid")
    (path / "README.txt").write_text("safe\n", encoding="utf-8")
    return path, _commit(path, "initial commit")


def _runtime_secret() -> str:
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(36))
    return "GITHUB_TOKEN=" + "gh" + "p_" + suffix + "\n"


def _scan(repo: Path, log_range: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{repo}:/repo:ro",
        _gitleaks_image(),
        "detect",
        "--source",
        "/repo",
        "--redact",
        "--verbose",
        "--exit-code",
        "1",
    ]
    if log_range is not None:
        command.append(f"--log-opts={log_range}")
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _scan_diagnostics(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_pull_request_range_ignores_secret_on_unrelated_branch(tmp_path: Path) -> None:
    repo, base_sha = _new_repo(tmp_path / "repo")

    _git(repo, "switch", "--create", "unrelated")
    (repo / "credentials.env").write_text(_runtime_secret(), encoding="utf-8")
    _commit(repo, "unrelated secret")

    _git(repo, "switch", "--create", "pull-request", base_sha)
    (repo / "feature.txt").write_text("clean pull request\n", encoding="utf-8")
    head_sha = _commit(repo, "clean pull request")

    result = _scan(repo, f"{base_sha}..{head_sha}")

    assert result.returncode == 0, _scan_diagnostics(result)


def test_pull_request_range_finds_secret_in_earlier_pull_request_commit(
    tmp_path: Path,
) -> None:
    repo, base_sha = _new_repo(tmp_path / "repo")
    _git(repo, "switch", "--create", "pull-request", base_sha)

    secret_path = repo / "credentials.env"
    secret_path.write_text(_runtime_secret(), encoding="utf-8")
    _commit(repo, "add secret")
    secret_path.unlink()
    head_sha = _commit(repo, "remove secret")

    result = _scan(repo, f"{base_sha}..{head_sha}")

    assert result.returncode == 1, _scan_diagnostics(result)


def test_unrestricted_scan_finds_secret_in_main_history(tmp_path: Path) -> None:
    repo, _ = _new_repo(tmp_path / "repo")

    secret_path = repo / "credentials.env"
    secret_path.write_text(_runtime_secret(), encoding="utf-8")
    _commit(repo, "add secret")
    secret_path.unlink()
    _commit(repo, "remove secret")

    result = _scan(repo)

    assert result.returncode == 1, _scan_diagnostics(result)


def test_workflow_limits_log_range_to_pull_requests() -> None:
    job = _gitleaks_job()
    assert job["name"] == "gitleaks (full history)"
    steps = job["steps"]
    assert isinstance(steps, list)

    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v7")
    assert checkout["with"]["fetch-depth"] == 0

    scope_steps = [
        step
        for step in steps
        if "--log-opts" in str(step.get("run", ""))
        and "github.event.pull_request.base.sha" in str(step.get("run", ""))
        and "github.event.pull_request.head.sha" in str(step.get("run", ""))
    ]
    assert len(scope_steps) == 1, "workflow must define one pull request base to head range"

    scope_step = scope_steps[0]
    condition = str(scope_step.get("if", "")).removeprefix("${{").removesuffix("}}").strip()
    assert condition in {
        "github.event_name == 'pull_request'",
        'github.event_name == "pull_request"',
    }, "the log range must be configured only for pull request events"

    scope_command = str(scope_step["run"])
    assert re.search(
        r"--log-opts=.*github\.event\.pull_request\.base\.sha.*"
        r"\.\..*github\.event\.pull_request\.head\.sha",
        scope_command,
        re.DOTALL,
    ), "the pull request range must run from the event base SHA to the event head SHA"
    assert "GITHUB_ENV" in scope_command

    scan_step = next(step for step in steps if step.get("name") == "Run gitleaks")
    assert "if" not in scan_step, "the unrestricted scan must still run for full history events"
    scan_command = str(scan_step["run"])
    assert re.search(
        r"docker run.*?detect .*?\$\{GITLEAKS_LOG_OPTS:\+\"\$GITLEAKS_LOG_OPTS\"\}",
        scan_command,
        re.DOTALL,
    ), "the gitleaks invocation must pass the pull request range only when it is set"
