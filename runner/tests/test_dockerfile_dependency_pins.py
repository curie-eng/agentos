from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "runner" / "Dockerfile"
_UV_LOCK = _REPO_ROOT / "uv.lock"
_EXACT_NPM_VERSION = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_NPM_PACKAGE = re.compile(r"(?:@[0-9A-Za-z._-]+/)?[0-9A-Za-z._-]+")
_PIP_EXECUTABLE = re.compile(r"pip(?:\d+(?:\.\d+)?)?$")
_NPM_INSTALL_ALIASES = {"install", "i", "add"}


@dataclass(frozen=True)
class Violation:
    package: str
    message: str


def _normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_instructions(dockerfile_text: str) -> list[str]:
    instructions: list[str] = []
    pending: list[str] = []
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continues = line.endswith("\\")
        pending.append(line[:-1].rstrip() if continues else line)
        if not continues:
            instructions.append(" ".join(pending))
            pending = []
    assert not pending, "Dockerfile ends with an unfinished continuation"
    return instructions


def _shell_tokens(instruction: str) -> list[str]:
    lexer = shlex.shlex(instruction, posix=True, punctuation_chars=";&|")
    lexer.commenters = ""
    lexer.whitespace_split = True
    return list(lexer)


def _locked_runner_dependencies(lock_text: str) -> dict[str, str]:
    lock = tomllib.loads(lock_text)
    packages = lock.get("package")
    assert isinstance(packages, list), "uv.lock must contain package records"
    assert all(isinstance(package, dict) for package in packages), (
        "uv.lock package records must be tables"
    )

    runner_records = [
        package
        for package in packages
        if _normalize_python_name(str(package.get("name", ""))) == "curie-runner"
    ]
    assert len(runner_records) == 1, "uv.lock must contain exactly one curie-runner record"

    dependencies = runner_records[0].get("dependencies")
    assert isinstance(dependencies, list), "curie-runner must declare dependencies in uv.lock"
    expected: dict[str, str] = {}
    for dependency in dependencies:
        assert isinstance(dependency, dict) and isinstance(dependency.get("name"), str), (
            "each curie-runner dependency must have a name"
        )
        name = _normalize_python_name(dependency["name"])
        resolved = [
            package
            for package in packages
            if _normalize_python_name(str(package.get("name", ""))) == name
        ]
        assert len(resolved) == 1, f"uv.lock must resolve {name} exactly once"
        source = resolved[0].get("source")
        assert isinstance(source, dict), f"uv.lock package {name} must declare its source"
        if "registry" not in source:
            continue
        version = resolved[0].get("version")
        assert isinstance(version, str) and version, (
            f"uv.lock registry package {name} must declare a version"
        )
        assert name not in expected, f"curie-runner dependency {name} is duplicated"
        expected[name] = version
    return expected


def _dockerfile_python_pins(instructions: list[str]) -> dict[str, list[str]]:
    pins: dict[str, list[str]] = {}
    controls = {"&&", "||", ";"}
    for instruction in instructions:
        tokens = _shell_tokens(instruction)
        if not tokens or tokens[0].upper() != "RUN":
            continue
        for index, token in enumerate(tokens[:-1]):
            if not _PIP_EXECUTABLE.fullmatch(token.rsplit("/", 1)[-1]) or (
                tokens[index + 1] != "install"
            ):
                continue
            for operand in tokens[index + 2 :]:
                if operand in controls:
                    break
                if operand.startswith("-") or "==" not in operand:
                    continue
                name, version = operand.split("==", 1)
                if name:
                    pins.setdefault(_normalize_python_name(name), []).append(version)
    return pins


def _dockerfile_python_operands(dockerfile_text: str) -> dict[str, str]:
    operands: dict[str, str] = {}
    controls = {"&&", "||", ";"}
    for instruction in _logical_instructions(dockerfile_text):
        tokens = _shell_tokens(instruction)
        if not tokens or tokens[0].upper() != "RUN":
            continue
        for index, token in enumerate(tokens[:-1]):
            if not _PIP_EXECUTABLE.fullmatch(token.rsplit("/", 1)[-1]) or (
                tokens[index + 1] != "install"
            ):
                continue
            for operand in tokens[index + 2 :]:
                if operand in controls:
                    break
                if operand.startswith("-") or "==" not in operand:
                    continue
                name, _ = operand.split("==", 1)
                package = _normalize_python_name(name)
                assert package not in operands, f"Dockerfile must pin {package} exactly once"
                operands[package] = operand
    return operands


def _split_npm_operand(operand: str) -> tuple[str, str | None]:
    version_at = operand.rfind("@")
    if version_at <= 0:
        return operand, None
    return operand[:version_at], operand[version_at + 1 :]


def _npm_violations(instructions: list[str]) -> list[Violation]:
    violations: list[Violation] = []
    controls = {"&&", "||", ";"}
    for instruction in instructions:
        tokens = _shell_tokens(instruction)
        if not tokens or tokens[0].upper() != "RUN":
            continue
        for index, token in enumerate(tokens[:-1]):
            if token.rsplit("/", 1)[-1] != "npm" or (
                tokens[index + 1] not in _NPM_INSTALL_ALIASES
            ):
                continue
            command: list[str] = []
            for operand in tokens[index + 2 :]:
                if operand in controls:
                    break
                command.append(operand)
            if not ({"-g", "--global"} & set(command)):
                continue
            for operand in command:
                if operand.startswith("-"):
                    continue
                package, version = _split_npm_operand(operand)
                if not _NPM_PACKAGE.fullmatch(package) or not (
                    version and _EXACT_NPM_VERSION.fullmatch(version)
                ):
                    violations.append(
                        Violation(package, "global npm install is missing an exact version")
                    )
    return violations


def _dockerfile_global_npm_operands(dockerfile_text: str) -> list[str]:
    operands: list[str] = []
    controls = {"&&", "||", ";"}
    for instruction in _logical_instructions(dockerfile_text):
        tokens = _shell_tokens(instruction)
        if not tokens or tokens[0].upper() != "RUN":
            continue
        for index, token in enumerate(tokens[:-1]):
            if token.rsplit("/", 1)[-1] != "npm" or (
                tokens[index + 1] not in _NPM_INSTALL_ALIASES
            ):
                continue
            command: list[str] = []
            for operand in tokens[index + 2 :]:
                if operand in controls:
                    break
                command.append(operand)
            if {"-g", "--global"} & set(command):
                operands.extend(
                    operand for operand in command if not operand.startswith("-")
                )
    return operands


def _find_violations(lock_text: str, dockerfile_text: str) -> list[Violation]:
    expected = _locked_runner_dependencies(lock_text)
    instructions = _logical_instructions(dockerfile_text)
    pins = _dockerfile_python_pins(instructions)
    violations = _npm_violations(instructions)

    for package, versions in pins.items():
        if len(versions) > 1:
            violations.append(Violation(package, "duplicate exact Dockerfile pin"))

    for package in expected.keys() - pins.keys():
        violations.append(
            Violation(
                package,
                f"missing exact Dockerfile pin for lock version {expected[package]}",
            )
        )
    for package in pins.keys() - expected.keys():
        for version in pins[package]:
            violations.append(
                Violation(
                    package,
                    f"exact Dockerfile pin {version} is not a direct registry dependency",
                )
            )
    for package in expected.keys() & pins.keys():
        for version in set(pins[package]):
            if version != expected[package]:
                violations.append(
                    Violation(
                        package,
                        f"expected lock version {expected[package]}, "
                        f"found Dockerfile version {version}",
                    )
                )

    return sorted(violations, key=lambda violation: (violation.package, violation.message))


def test_actual_runner_dockerfile_matches_locked_dependencies() -> None:
    violations = _find_violations(
        _UV_LOCK.read_text(encoding="utf-8"),
        _DOCKERFILE.read_text(encoding="utf-8"),
    )
    assert violations == []


def test_prechange_drift_reports_all_four_violations() -> None:
    dockerfile = """\
RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @modelcontextprotocol/server-github
RUN /app/.venv/bin/pip install \\
    "claude-agent-sdk==0.2.115" \\
    "aiohttp==3.14.1" \\
    "opentelemetry-sdk==1.44.0" \\
    "opentelemetry-exporter-otlp-proto-http==1.44.0" \\
    "anyio==4.14.1"
"""
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    expected = _locked_runner_dependencies(lock_text)
    violations = _find_violations(lock_text, dockerfile)
    assert violations == [
        Violation(
            "@anthropic-ai/claude-code",
            "global npm install is missing an exact version",
        ),
        Violation(
            "@modelcontextprotocol/server-github",
            "global npm install is missing an exact version",
        ),
        Violation(
            "aiohttp",
            "expected lock version "
            f"{expected['aiohttp']}, found Dockerfile version 3.14.1",
        ),
        Violation(
            "claude-agent-sdk",
            "expected lock version "
            f"{expected['claude-agent-sdk']}, found Dockerfile version 0.2.115",
        ),
    ]


def test_python_pin_revert_is_rejected() -> None:
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    expected = _locked_runner_dependencies(lock_text)
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    current = _dockerfile_python_operands(dockerfile)["claude-agent-sdk"]
    assert dockerfile.count(current) == 1
    stale_version = f"{expected['claude-agent-sdk']}.stale"
    mutated = dockerfile.replace(current, f"claude-agent-sdk=={stale_version}")

    violations = _find_violations(lock_text, mutated)
    assert Violation(
        "claude-agent-sdk",
        "expected lock version "
        f"{expected['claude-agent-sdk']}, found Dockerfile version {stale_version}",
    ) in violations


def test_npm_version_removal_is_rejected() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    npm_operands = _dockerfile_global_npm_operands(dockerfile)
    assert len(npm_operands) >= 2
    for current in npm_operands:
        package, version = _split_npm_operand(current)
        assert version and _EXACT_NPM_VERSION.fullmatch(version)
        assert dockerfile.count(current) == 1
        mutated = dockerfile.replace(current, package)

        violations = _find_violations(_UV_LOCK.read_text(encoding="utf-8"), mutated)
        assert Violation(
            package, "global npm install is missing an exact version"
        ) in violations


@pytest.mark.parametrize("command", sorted(_NPM_INSTALL_ALIASES))
def test_npm_global_install_aliases_require_exact_versions(command: str) -> None:
    violations = _find_violations(
        _UV_LOCK.read_text(encoding="utf-8"),
        f"RUN npm {command} -g @example/tool\n",
    )
    assert Violation(
        "@example/tool", "global npm install is missing an exact version"
    ) in violations


@pytest.mark.parametrize("executable", ["pip", "pip3"])
def test_pip_executable_forms_are_compared_with_the_lock(executable: str) -> None:
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    operands = _dockerfile_python_operands(dockerfile)
    synthetic = "RUN " + executable + " install " + " ".join(
        f'"{operand}"' for operand in operands.values()
    )

    assert _find_violations(lock_text, synthetic) == []


def test_python_pin_set_must_match_all_direct_registry_dependencies() -> None:
    lock_text = _UV_LOCK.read_text(encoding="utf-8")
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    expected = _locked_runner_dependencies(lock_text)
    operands = _dockerfile_python_operands(dockerfile)
    assert operands.keys() == expected.keys()
    baseline = "RUN pip install " + " ".join(
        f'"{operand}"' for operand in operands.values()
    )
    missing = baseline.replace(f' "{operands["anyio"]}"', "")
    extra = baseline + ' "httpx==1.0.0"'
    duplicate = baseline + f' "{operands["aiohttp"]}"'

    assert Violation(
        "anyio", f"missing exact Dockerfile pin for lock version {expected['anyio']}"
    ) in _find_violations(lock_text, missing)
    assert Violation(
        "httpx", "exact Dockerfile pin 1.0.0 is not a direct registry dependency"
    ) in _find_violations(lock_text, extra)
    assert Violation("aiohttp", "duplicate exact Dockerfile pin") in _find_violations(
        lock_text, duplicate
    )
