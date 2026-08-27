"""Regression coverage for dependencies installed in the API image."""

import tomllib
from pathlib import Path


def test_api_declares_imported_telemetry_workspace_packages() -> None:
    """Package-scoped image installs must include both telemetry runtimes."""
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text())["project"]["dependencies"]

    assert "curie-telemetry" in dependencies
    assert "curie-telemetry-schema" in dependencies
