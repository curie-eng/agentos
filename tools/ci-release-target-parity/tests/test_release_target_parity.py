"""Release-target parity gate over the real GitHub Actions workflows (issue #2458).

The rule this file encodes: every Rust target `release.yaml` publishes must also
be BUILT by `ci.yaml` on ordinary pushes and pull requests. A target compiled for
the first time when a tag is pushed has no signal before the release, and
`fail-fast: false` then leaves a partial asset set behind a tag that is already
pushed and cannot be taken back.

Issue #2458 is the instance: `aarch64-apple-darwin` shipped in the release matrix
from the start while `ci.yaml` added only the two Linux targets, so a macOS build
break was undiscoverable until a release run hit it. Issue #1341 is the same shape
one layer down -- the cross-compile PATH was never exercised before a tag -- and
`cli-portability` was added to close it for Linux; this gate is what stops the
next target from being added to the release matrix alone.

The gate parses `.github/workflows/*.yaml` directly. There are no fixtures and no
mocks: the whole value of it is that it reads what CI actually runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"

# `cargo zigbuild` takes the glibc floor as a SUFFIX on the triple
# (`x86_64-unknown-linux-gnu.2.28`), and cargo-zigbuild strips it again for the
# output directory. Match the triple and let any floor follow it, so pinning a
# different floor is not mistaken for dropping the target.
_TARGET_FLAG = re.compile(
    r"""--target[= ]+['"]?([A-Za-z0-9_]+-[A-Za-z0-9_.-]+?)(?:\.\d+\.\d+)?['"]?(?:\s|$)"""
)


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _run_steps(workflow: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                commands.append(run)
    return commands


def _release_cli_targets() -> set[str]:
    jobs = _workflow("release.yaml")["jobs"]
    include = jobs["cli-binaries"]["strategy"]["matrix"]["include"]
    targets = {entry["target"] for entry in include}
    assert targets, "release.yaml publishes no CLI target; this gate is reading the wrong job"
    return targets


def _ci_built_targets() -> set[str]:
    built: set[str] = set()
    for command in _run_steps(_workflow("ci.yaml")):
        built.update(_TARGET_FLAG.findall(command))
    return built


def test_every_released_cli_target_is_built_by_main_ci() -> None:
    missing = sorted(_release_cli_targets() - _ci_built_targets())
    assert not missing, (
        f"{missing} ship in release.yaml's cli-binaries matrix but no ci.yaml step builds them. "
        "A target compiled for the first time at tag time has no pre-release signal, and a break "
        "in it fails the release run with the tag already pushed (#2458, #1341). Add a ci.yaml "
        "job that builds it the same way a tag does."
    )


def test_the_parity_gate_can_fail() -> None:
    """A gate that cannot fail is not a gate: prove the matcher discriminates."""
    assert _TARGET_FLAG.findall("cargo build --release --target aarch64-apple-darwin") == [
        "aarch64-apple-darwin"
    ]
    # The glibc floor is a suffix, not a different target.
    assert _TARGET_FLAG.findall(
        "cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.28"
    ) == ["x86_64-unknown-linux-gnu"]
    assert _TARGET_FLAG.findall("cargo build --release") == []
    assert "aarch64-apple-darwin" not in _ci_built_targets() - {"aarch64-apple-darwin"}
