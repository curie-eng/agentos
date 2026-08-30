"""Executable contract for the required fix pin pull request gate.

The helper is intentionally exercised as a subprocess. Its observable contract
is a pull request body, an event action, and an argv call to the already built
``curie`` binary. The workflow assertions read CI itself because a helper that
is never called cannot protect a merge.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "tools" / "fix-pin-ci" / "check.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
VERIFY_FIX_PIN = REPO_ROOT / "cli" / "scripts" / "verify-fix-pin.sh"

VALID_SELECTOR = "apps/api/tests/test_fix_pin_ci_gate.py::test_exact_declaration"
PR_CONDITION = re.compile(r"github\.event_name\s*==\s*['\"]pull_request['\"]")


def _write_event(
    tmp_path: Path,
    body: str | None,
    *,
    action: str = "opened",
    include_body: bool = True,
) -> Path:
    pull_request: dict[str, object] = {}
    if include_body:
        pull_request["body"] = body
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"action": action, "pull_request": pull_request}), encoding="utf-8"
    )
    return event_path


def _write_fake_curie(tmp_path: Path) -> tuple[Path, Path]:
    call_log = tmp_path / "curie-argv.json"
    fake_curie = tmp_path / "curie"
    fake_curie.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import os",
                "from pathlib import Path",
                "import sys",
                "Path(os.environ['FIX_PIN_CALL_LOG']).write_text(json.dumps(sys.argv[1:]))",
                "print(os.environ.get('FIX_PIN_OUTPUT', ''), end='')",
                "raise SystemExit(int(os.environ.get('FIX_PIN_EXIT', '0')))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_curie.chmod(0o755)
    return fake_curie, call_log


def _run_checker(
    tmp_path: Path,
    body: str | None,
    *,
    action: str = "opened",
    include_body: bool = True,
    verifier_exit: int = 0,
    verifier_stdout: str = "PINNED\n",
    timeout: float | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    event_path = _write_event(tmp_path, body, action=action, include_body=include_body)
    curie, call_log = _write_fake_curie(tmp_path)
    environment = {
        **os.environ,
        "FIX_PIN_CALL_LOG": str(call_log),
        "FIX_PIN_EXIT": str(verifier_exit),
        "FIX_PIN_OUTPUT": verifier_stdout,
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--event",
            str(event_path),
            "--curie",
            str(curie),
            "--ref",
            "HEAD",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=timeout,
    )
    return completed, call_log


@pytest.mark.parametrize(
    ("body", "include_body"),
    [
        (None, True),
        (None, False),
        ("<!-- Fix pin: apps/api/tests/test_example.py::test_example -->", True),
        ("<!-- Fix pin: apps/api/tests/test_example.py::test_example -->\r\n", True),
    ],
)
def test_non_fix_pull_requests_skip_without_calling_the_verifier(
    tmp_path: Path, body: str | None, include_body: bool
) -> None:
    completed, call_log = _run_checker(tmp_path, body, include_body=include_body)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SKIPPED: no Fix pin declaration"
    assert not call_log.exists(), "a non fix pull request must not run curie"


def test_exact_declaration_calls_the_verifier_with_one_argv_selector(tmp_path: Path) -> None:
    completed, call_log = _run_checker(tmp_path, f"Fix pin: {VALID_SELECTOR}\n")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(call_log.read_text(encoding="utf-8")) == [
        "dev",
        "verify-fix-pin",
        "HEAD",
        VALID_SELECTOR,
    ]


@pytest.mark.parametrize(
    "selector",
    [
        "apps/worker/tests/kernel/test_consumer.py::test_consumes_stream_entry_end_to_end_and_acks",
        "runner/tests/test_history.py::test_example",
        (
            "apps/api/tests/test_config_parity.py::"
            "TestResumeDeadLetterStreamCoherence::"
            "test_empty_resume_override_falls_back_to_the_shared_graveyard"
        ),
        (
            "apps/worker/tests/reconcile/test_connector_drift.py::"
            "test_no_single_kind_reports_drift_on_its_own[Service]"
        ),
    ],
)
def test_supported_nested_python_selectors_call_the_verifier(
    tmp_path: Path, selector: str
) -> None:
    completed, call_log = _run_checker(tmp_path, f"Fix pin: {selector}")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(call_log.read_text(encoding="utf-8")) == [
        "dev",
        "verify-fix-pin",
        "HEAD",
        selector,
    ]


def test_ordinary_fix_pin_prose_skips_without_calling_the_verifier(tmp_path: Path) -> None:
    completed, call_log = _run_checker(
        tmp_path, "This closes the fix pin-related enforcement gap"
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SKIPPED: no Fix pin declaration"
    assert not call_log.exists(), "ordinary prose must not run curie"


def test_long_decoration_prefix_in_ordinary_prose_skips_within_timeout(
    tmp_path: Path,
) -> None:
    completed, call_log = _run_checker(
        tmp_path,
        "#" * 24 + " ordinary prose",
        timeout=1.0,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SKIPPED: no Fix pin declaration"
    assert not call_log.exists(), "ordinary prose must not run curie"


@pytest.mark.parametrize(
    "body",
    [
        "Fix pin:",
        f" Fix pin: {VALID_SELECTOR}",
        f"Fix pin: {VALID_SELECTOR} with explanation",
        f"Fix pin: {VALID_SELECTOR}\nFix pin: cli/tests/verify_fix_pin.rs::ci_gate_pins",
    ],
)
def test_invalid_or_duplicate_declarations_fail_closed_without_calling_curie(
    tmp_path: Path, body: str
) -> None:
    completed, call_log = _run_checker(tmp_path, body)

    assert completed.returncode != 0
    assert "Fix pin declaration" in f"{completed.stdout}\n{completed.stderr}"
    assert not call_log.exists(), "invalid policy input must not reach curie"


@pytest.mark.parametrize(
    "selector",
    [
        "--help",
        "-h",
        "cli/tests/verify_fix_pin.rs::--no-run",
        "tools/fix-pin-ci/tests/test_fix_pin_ci.py::test_not_supported",
        "apps/api/tests/test_fix_pin_ci_gate.py",
    ],
)
def test_unsupported_selectors_fail_closed_without_calling_curie(
    tmp_path: Path, selector: str
) -> None:
    completed, call_log = _run_checker(tmp_path, f"Fix pin: {selector}")

    assert completed.returncode != 0
    assert "Fix pin declaration" in f"{completed.stdout}\n{completed.stderr}"
    assert not call_log.exists(), "unsupported selectors must not reach curie"


@pytest.mark.parametrize(
    "body",
    [
        f"Fix Pin: {VALID_SELECTOR}",
        f"fix pin: {VALID_SELECTOR}",
        f"Fix-pin: {VALID_SELECTOR}",
        f"Fix pin : {VALID_SELECTOR}",
    ],
)
def test_near_miss_declaration_markers_fail_closed_without_calling_curie(
    tmp_path: Path, body: str
) -> None:
    completed, call_log = _run_checker(tmp_path, body)

    assert completed.returncode != 0
    assert "Fix pin declaration" in f"{completed.stdout}\n{completed.stderr}"
    assert not call_log.exists(), "near miss declarations must not reach curie"


@pytest.mark.parametrize(
    "body",
    [
        f"- Fix pin: {VALID_SELECTOR}",
        f"- [ ] Fix pin: {VALID_SELECTOR}",
        f"* [x] Fix pin: {VALID_SELECTOR}",
        f"> Fix pin: {VALID_SELECTOR}",
        f"**Fix pin: {VALID_SELECTOR}**",
        f"# Fix pin: {VALID_SELECTOR}",
        f"1. Fix pin: {VALID_SELECTOR}",
    ],
)
def test_markdown_decorated_declarations_fail_closed_without_calling_curie(
    tmp_path: Path, body: str
) -> None:
    completed, call_log = _run_checker(tmp_path, body)

    assert completed.returncode != 0
    assert "Fix pin declaration" in f"{completed.stdout}\n{completed.stderr}"
    assert not call_log.exists(), "decorated declarations must not reach curie"


def test_shell_metacharacters_fail_before_the_verifier_runs(tmp_path: Path) -> None:
    marker = tmp_path / "shell-was-run"
    selector = f"apps/api/tests/test_fix_pin_gate.py::test_safe;touch${{IFS}}{marker}"
    completed, call_log = _run_checker(tmp_path, f"Fix pin: {selector}", verifier_exit=97)

    assert completed.returncode != 0
    assert not call_log.exists(), "invalid selectors must not reach curie"
    assert not marker.exists(), "the declaration must never be interpolated into a shell command"


@pytest.mark.parametrize("verifier_stdout", ["", "NOT PINNED\n", "PINNED extra\n"])
def test_verifier_exit_zero_requires_an_exact_pinned_marker(
    tmp_path: Path, verifier_stdout: str
) -> None:
    completed, call_log = _run_checker(
        tmp_path,
        f"Fix pin: {VALID_SELECTOR}",
        verifier_stdout=verifier_stdout,
    )

    assert completed.returncode != 0
    assert call_log.exists(), "a valid declaration must reach curie"


def test_changed_selected_python_test_is_pinned_by_real_pytest_junit_failure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    test_path = repository / "apps" / "api" / "tests" / "test_pin.py"
    source_path = repository / "apps" / "api" / "pin_fixture.py"
    test_path.parent.mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        """[project]
name = "pin-fixture"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []

[dependency-groups]
dev = ["pytest>=8.3"]

[tool.uv]
package = false

[tool.pytest.ini_options]
pythonpath = ["."]
""",
        encoding="utf-8",
    )
    for package in (
        repository / "apps" / "__init__.py",
        repository / "apps" / "api" / "__init__.py",
        repository / "apps" / "api" / "tests" / "__init__.py",
    ):
        package.write_text("", encoding="utf-8")
    source_path.write_text("def value():\n    return 1\n", encoding="utf-8")
    test_path.write_text(
        """from apps.api.pin_fixture import value


def test_selected():
    assert value() == 1
""",
        encoding="utf-8",
    )

    git_command = [
        "git",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    for arguments in (
        ["init", "-q"],
        ["config", "user.name", "Curie Test"],
        ["config", "user.email", "curie@example.com"],
        ["add", "."],
        ["commit", "-q", "-m", "Add Python fixture"],
    ):
        subprocess.run(
            [*git_command, *arguments],
            cwd=repository,
            check=True,
        )

    source_path.write_text("def value():\n    return 2\n", encoding="utf-8")
    test_path.write_text(
        """from apps.api.pin_fixture import value


def test_selected():
    assert value() == 2
""",
        encoding="utf-8",
    )
    for arguments in (
        ["add", "."],
        ["commit", "-q", "-m", "Fix Python behavior"],
    ):
        subprocess.run(
            [*git_command, *arguments],
            cwd=repository,
            check=True,
        )
    fix_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()

    completed = subprocess.run(
        [
            "bash",
            str(VERIFY_FIX_PIN),
            fix_commit,
            "apps/api/tests/test_pin.py::test_selected",
        ],
        cwd=repository,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
    )
    shown = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode == 0, shown
    assert "PINNED" in completed.stdout.splitlines(), shown
    assert "1 failed" in shown, shown


def test_committed_pull_request_template_skips_without_calling_curie(tmp_path: Path) -> None:
    completed, call_log = _run_checker(tmp_path, PR_TEMPLATE.read_text(encoding="utf-8"))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SKIPPED: no Fix pin declaration"
    assert not call_log.exists(), "the template instruction must not activate the verifier"


def _load_ci() -> dict[str, Any]:
    document = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "ci.yaml must be a YAML mapping"
    return document


def _workflow_trigger(document: dict[str, Any]) -> dict[str, Any]:
    trigger = document.get("on", document.get(True))
    assert isinstance(trigger, dict), "ci.yaml must declare an on mapping"
    return trigger


def _python_job(document: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), "ci.yaml must declare jobs"
    job = jobs.get("python")
    assert isinstance(job, dict), "ci.yaml must retain the python job"
    steps = job.get("steps")
    assert isinstance(steps, list), "the required Python job must retain steps"
    return job, [step for step in steps if isinstance(step, dict)]


def _single_step_index(
    steps: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool], description: str
) -> int:
    matches = [index for index, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one {description}, found {len(matches)}"
    return matches[0]


def _string(step: dict[str, Any], key: str) -> str:
    value = step.get(key)
    return value if isinstance(value, str) else ""


def _pull_request_only(step: dict[str, Any]) -> bool:
    return bool(PR_CONDITION.search(_string(step, "if")))


def test_ci_keeps_the_required_python_status_and_calls_fix_pin_after_pytest() -> None:
    document = _load_ci()
    trigger = _workflow_trigger(document)
    pull_request = trigger.get("pull_request")
    assert isinstance(pull_request, dict), "CI must run for pull requests"
    actions = pull_request.get("types")
    assert isinstance(actions, list), "CI must declare its pull request actions"
    assert len(actions) == 4 and set(actions) == {
        "opened",
        "synchronize",
        "reopened",
        "edited",
    }, "CI must rerun required checks when code or the pull request body changes"

    job, steps = _python_job(document)
    assert job.get("name") == "Python (ruff + mypy + pytest)"
    assert "needs" not in job, "the required Python check must not be skippable"

    checkout_index = _single_step_index(
        steps,
        lambda step: _string(step, "uses") == "actions/checkout@v7",
        "Python checkout",
    )
    checkout = steps[checkout_index]
    checkout_with = checkout.get("with")
    assert isinstance(checkout_with, dict)
    assert checkout_with.get("fetch-depth") == 0
    assert checkout_with.get("persist-credentials") is False

    stack_index = _single_step_index(
        steps,
        lambda step: "docker compose -f compose.dev.yaml up -d" in _string(step, "run"),
        "dev stack startup",
    )
    migration_index = _single_step_index(
        steps,
        lambda step: "uv run alembic upgrade head" in _string(step, "run"),
        "shared database migration",
    )
    pytest_index = _single_step_index(
        steps,
        lambda step: _string(step, "run").strip() == "uv run pytest -q",
        "normal Python suite",
    )
    gate_index = _single_step_index(
        steps,
        lambda step: "tools/fix-pin-ci/check.py" in _string(step, "run"),
        "fix pin caller",
    )
    gate = steps[gate_index]
    assert _pull_request_only(gate), "the verifier must not run for pushes"
    assert stack_index < migration_index < pytest_index < gate_index

    gate_environment = gate.get("env")
    assert isinstance(gate_environment, dict)
    assert gate_environment.get("CARGO_TARGET_DIR") == "${{ github.workspace }}/cli/target"
    assert shlex.split(_string(gate, "run")) == [
        "python3",
        "tools/fix-pin-ci/check.py",
        "--event",
        "$GITHUB_EVENT_PATH",
        "--curie",
        "cli/target/release/curie",
        "--ref",
        "HEAD",
    ]
    assert '--event "$GITHUB_EVENT_PATH"' in _string(gate, "run")

    release_build_index = _single_step_index(
        steps,
        lambda step: _string(step, "run").strip()
        == "cargo build --release --locked --manifest-path cli/Cargo.toml",
        "direct current release build",
    )
    helm_index = _single_step_index(
        steps,
        lambda step: _string(step, "uses")
        == "azure/setup-helm@9bc31f4ebc9c6b171d7bfbaa5d006ae7abdb4310"
        and (step.get("with") or {}).get("version") == "v3.16.4",
        "pinned Helm setup",
    )
    for index in (release_build_index, helm_index):
        assert _pull_request_only(steps[index]), "selector tooling must not affect push runs"
        assert pytest_index < index < gate_index
    assert release_build_index < helm_index < gate_index
    assert not any(
        _string(step, "uses") == "Swatinem/rust-cache@v2" for step in steps
    ), "the Python job must build directly without a Cargo cache dependency"
    assert not any(
        _string(step, "uses") == "actions/download-artifact@v8" for step in steps
    ), "the Python job must not download its curie binary"
    assert not any(
        "chmod +x cli/target/release/curie" in _string(step, "run") for step in steps
    ), "the direct Cargo build creates the executable"

    diagnostic_index = _single_step_index(
        steps,
        lambda step: "docker compose -f compose.dev.yaml logs" in _string(step, "run")
        and _string(step, "if") == "failure()",
        "failure stack diagnostic",
    )
    assert gate_index < diagnostic_index


def test_pull_request_template_documents_the_opt_in_declaration() -> None:
    template = PR_TEMPLATE.read_text(encoding="utf-8")

    assert "## Fix pin verification" in template
    assert "Fix pin: <supported selector>" in template
    for selector_shape in (
        "apps/*/tests/*.py::test",
        "packages/*/tests/*.py::test",
        "cli/tests/name.rs::test",
        "charts/curie/ci/name.sh",
    ):
        assert selector_shape in template
    assert re.search(r"non.fix.*leave.*empty", template, flags=re.IGNORECASE | re.DOTALL)
    assert re.search(r"one.*selector.*changed", template, flags=re.IGNORECASE | re.DOTALL)
