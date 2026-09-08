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
mocks: the whole value of it is that it reads what CI actually runs. Its negative
control doctors a copy of the real `ci.yaml` and drives the same assertion the
real check uses, rather than testing the matcher in isolation.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"

# A target only counts when a cargo BUILD command names it. Mentioning a triple
# is not building it: `rustup target add` installs the std rlibs, `echo` and a
# commented-out line say nothing at all, and a gate that counted those would go
# green on a job whose build step had been commented out.
_BUILD = re.compile(r"\bcargo\s+(?:\+\S+\s+)?(?:build|zigbuild)\b")

# `cargo zigbuild` takes the glibc floor as a SUFFIX on the triple
# (`x86_64-unknown-linux-gnu.2.28`), and cargo-zigbuild strips it again for the
# output directory. Match the triple and let any floor follow it, so pinning a
# different floor is not mistaken for dropping the target.
_TARGET = re.compile(
    r"""--target[= ]+['"]?([A-Za-z0-9_]+-[A-Za-z0-9_.-]+?)(?:\.\d+\.\d+)?['"]?(?:\s|$)"""
)


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _run_steps(workflow: dict[str, Any]) -> Iterator[str]:
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                yield run


def _commands(run: str) -> Iterator[str]:
    """The executable lines of one `run:` block.

    Backslash continuations are joined first, so a build split across lines is
    still one command. Whole-line comments are then dropped: a build step that
    has been commented out must read as absent, which is the case that made the
    first version of this gate pass on a workflow that built nothing.
    """
    for line in re.sub(r"\\\n", " ", run).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


def _release_cli_targets() -> set[str]:
    jobs = _workflow("release.yaml")["jobs"]
    assert "cli-binaries" in jobs, (
        "release.yaml has no cli-binaries job. Either it was renamed -- update this gate with it "
        "-- or this gate is reading the wrong workflow."
    )
    targets: set[str] = set()
    for job in jobs.values():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        for entry in matrix.get("include") or []:
            if isinstance(entry, dict) and entry.get("target"):
                targets.add(entry["target"])
    assert targets, "release.yaml publishes no CLI target; this gate is reading the wrong job"
    return targets


def _built_targets(workflow: dict[str, Any]) -> set[str]:
    built: set[str] = set()
    for run in _run_steps(workflow):
        for command in _commands(run):
            if _BUILD.search(command):
                built.update(_TARGET.findall(command))
    return built


def _unbuilt(ci: dict[str, Any]) -> list[str]:
    return sorted(_release_cli_targets() - _built_targets(ci))


def test_every_released_cli_target_is_built_by_main_ci() -> None:
    missing = _unbuilt(_workflow("ci.yaml"))
    assert not missing, (
        f"{missing} ship in release.yaml's build matrices but no ci.yaml step builds them. "
        "A target compiled for the first time at tag time has no pre-release signal, and a break "
        "in it fails the release run with the tag already pushed (#2458, #1341). Add a ci.yaml "
        "job that builds it the same way a tag does."
    )


def test_the_gate_rejects_a_build_that_was_commented_out() -> None:
    """The negative control, driven through the real assertion.

    A gate that cannot fail is not a gate, and the failure that matters is the
    realistic one: the job is still there, its build line is not.
    """
    raw = (WORKFLOWS / "ci.yaml").read_text()
    build = "run: cargo build --release --target aarch64-apple-darwin"
    assert raw.count(build) == 1, "the darwin build step moved; update this control"
    doctored = yaml.safe_load(raw.replace(build, f'run: "# {build.removeprefix("run: ")}"'))

    assert _unbuilt(doctored) == ["aarch64-apple-darwin"]


def test_naming_a_target_is_not_building_it() -> None:
    def ci(command: str) -> dict[str, Any]:
        return {"jobs": {"a-job": {"steps": [{"run": command}]}}}

    # Installing the std rlibs for a target is a prerequisite of building it, not
    # the build. `cli-portability` really does add both Linux targets this way.
    assert _built_targets(
        ci("rustup target add x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu")
    ) == set()
    assert _built_targets(ci("echo --target aarch64-apple-darwin")) == set()
    assert _built_targets(ci("cargo build --release --target aarch64-apple-darwin")) == {
        "aarch64-apple-darwin"
    }
    # The glibc floor is a suffix, not a different target.
    floored = ci("cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.28")
    assert _built_targets(floored) == {"x86_64-unknown-linux-gnu"}
    # A build split across lines is still one build.
    assert _built_targets(ci("cargo build --release \\\n  --target aarch64-apple-darwin")) == {
        "aarch64-apple-darwin"
    }
