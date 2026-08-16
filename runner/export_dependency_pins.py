"""Export the runner's direct registry dependencies from a uv lockfile."""

from __future__ import annotations

import re
import sys
import tomllib
from typing import Any


class InvalidLock(ValueError):
    """Raised when the runner dependency records cannot be trusted."""


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _require_package_records(lock: dict[str, Any]) -> list[dict[str, Any]]:
    packages = lock.get("package")
    if not isinstance(packages, list) or not all(
        isinstance(package, dict) for package in packages
    ):
        raise InvalidLock("uv.lock must contain package records")
    return packages


def _package_name(package: dict[str, Any], context: str) -> str:
    name = package.get("name")
    if not isinstance(name, str) or not (normalized := _normalize_name(name)):
        raise InvalidLock(f"{context} must declare a package name")
    return normalized


def _runner_dependency_pins(lock: dict[str, Any]) -> list[str]:
    packages = _require_package_records(lock)
    runners = [
        package
        for package in packages
        if _package_name(package, "uv.lock package record") == "curie-runner"
    ]
    if len(runners) != 1:
        raise InvalidLock("uv.lock must contain exactly one curie-runner record")

    dependencies = runners[0].get("dependencies")
    if not isinstance(dependencies, list):
        raise InvalidLock("curie-runner must declare dependency records")

    package_by_name: dict[str, dict[str, Any]] = {}
    for package in packages:
        name = _package_name(package, "uv.lock package record")
        if name in package_by_name:
            raise InvalidLock(f"uv.lock resolves {name} more than once")
        package_by_name[name] = package

    dependency_names: set[str] = set()
    pins: dict[str, str] = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise InvalidLock("each curie-runner dependency must be a package record")
        name = _package_name(dependency, "each curie-runner dependency")
        if name in dependency_names:
            raise InvalidLock(f"curie-runner dependency {name} is duplicated")
        dependency_names.add(name)

        resolved = package_by_name.get(name)
        if resolved is None:
            raise InvalidLock(f"uv.lock does not resolve curie-runner dependency {name}")
        source = resolved.get("source")
        if not isinstance(source, dict):
            raise InvalidLock(f"uv.lock package {name} must declare its source")
        if "registry" not in source:
            continue

        version = resolved.get("version")
        if not isinstance(version, str) or not version:
            raise InvalidLock(f"uv.lock registry package {name} must declare a version")
        pins[name] = version

    return [f"{name}=={version}" for name, version in sorted(pins.items())]


def main() -> int:
    try:
        lock = tomllib.loads(sys.stdin.read())
        print("\n".join(_runner_dependency_pins(lock)))
    except (InvalidLock, tomllib.TOMLDecodeError) as error:
        print(f"invalid uv.lock: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
