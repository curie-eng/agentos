"""Executable regression for PR bodies that defeat GitHub auto-close (#1713)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "check-pr-body.sh"


def _check_body(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    body_path = tmp_path / "pull-request-body.md"
    body_path.write_text(body, encoding="utf-8")
    return subprocess.run(
        ["bash", str(CHECKER), str(body_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _diagnostics(completed: subprocess.CompletedProcess[str]) -> str:
    return (
        f"exit code: {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


@pytest.mark.parametrize(
    "body",
    (
        "Describe a maintenance change without a closing keyword.\n",
        "Describe a fix.\n\nCloses #1713\n",
    ),
)
def test_pr_body_check_accepts_plain_text_and_real_newlines(tmp_path: Path, body: str) -> None:
    completed = _check_body(tmp_path, body)

    assert completed.returncode == 0, _diagnostics(completed)


def test_pr_body_check_rejects_literal_backslash_n_before_it_inerts_a_closing_keyword(
    tmp_path: Path,
) -> None:
    completed = _check_body(tmp_path, "Describe a fix.\\n\\nCloses #1713\n")
    diagnostics = _diagnostics(completed)

    assert completed.returncode != 0, diagnostics
    assert r"\n" in diagnostics, diagnostics
    assert "replace" in diagnostics.lower(), diagnostics
