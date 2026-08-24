"""Security-boundary tests for trusted publication snapshot validation."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any


def test_publication_git_environment_drops_ambient_credentials_and_config(
    monkeypatch: Any, tmp_path: Path
) -> None:
    validation = importlib.import_module("curie_worker.publication_validation")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret")
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: ambient-secret")

    env = validation.publication_git_environment(tmp_path)

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_publication_validation_refuses_workflow_changes() -> None:
    validation = importlib.import_module("curie_worker.publication_validation")

    assert not validation._safe_changed_path(".github/workflows/publish.yml")
    assert validation._safe_changed_path("src/main.py")
