"""Executable regressions for the pull request body guards.

Two rules share this checker: bodies that defeat GitHub auto-close (#1713),
and bodies that claim AI authorship (#962). The second half exists because
AGENTS.md forbade attribution on both surfaces while only the commit half was
enforced, so footers kept reaching merged pull requests.
"""

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


FOOTER = "\N{ROBOT FACE} Generated with [Claude Code](https://claude.com/claude-code)"


@pytest.mark.parametrize(
    "body",
    (
        # The exact footer that reached merged pull requests.
        f"Describe a fix.\n\n{FOOTER}\n",
        # The same footer with no trailing newline, which is the shape a body
        # pasted straight out of a tool actually has. A `while read` loop drops
        # that last line unless it is written to handle it.
        f"Describe a fix.\n\n{FOOTER}",
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n",
        "This patch was generated with Claude\n",
    ),
)
def test_pr_body_check_rejects_ai_attribution(tmp_path: Path, body: str) -> None:
    completed = _check_body(tmp_path, body)

    assert completed.returncode != 0, _diagnostics(completed)
    assert "AI" in completed.stderr, _diagnostics(completed)


@pytest.mark.parametrize(
    "body",
    (
        # Attribution, not mention. These are this repo's ordinary vocabulary and
        # must stay mergeable, or the gate gets switched off.
        "Update apps/ui/CLAUDE.md and the harness.claude import boundary.\n",
        "Pin claude-agent-sdk==0.2.110 so claude_agent_sdk spans stay schema-clean.\n",
        "Describe the Claude harness port and its registry.\n\nCloses #1713\n",
    ),
)
def test_pr_body_check_accepts_technical_references_to_the_harness(
    tmp_path: Path, body: str
) -> None:
    completed = _check_body(tmp_path, body)

    assert completed.returncode == 0, _diagnostics(completed)


def test_pr_body_guard_delegates_to_one_matcher() -> None:
    """Both surfaces must share a matcher, or the vendor lists drift apart."""
    checker = CHECKER.read_text(encoding="utf-8")

    assert "check-commit-messages.sh" in checker
    assert "--message-file" in checker


def test_pr_body_self_test_covers_the_attribution_half() -> None:
    """CI trusts --self-test before running the gate, so it must not be vacuous."""
    completed = subprocess.run(
        ["bash", str(CHECKER), "--self-test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, _diagnostics(completed)
    assert "AI attribution" in completed.stdout, _diagnostics(completed)


def test_pr_body_gate_inherits_the_commit_matcher_boundary(tmp_path: Path) -> None:
    """A bare name followed by `.` is out of scope, by the shared matcher's design.

    BARE_ASSISTANT excludes a trailing `[._-]` so `harness.claude`, `CLAUDE.md`
    and `claude-sonnet-5` never trip rule C. A sentence that ends "generated with
    Claude." rides that same exclusion. Widening it belongs to the commit gate
    and its false-positive budget, not here; this pins the boundary so the next
    reader knows it is inherited rather than overlooked. The footer forms that
    actually reach pull requests are caught by rule B regardless.
    """
    completed = _check_body(tmp_path, "This patch was generated with Claude.\n")

    assert completed.returncode == 0, _diagnostics(completed)
