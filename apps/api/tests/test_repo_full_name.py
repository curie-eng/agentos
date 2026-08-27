"""Repository binding validation at persistence and clone URL boundaries."""

import json
from pathlib import Path
from typing import Any

import pytest
from curie_api.config import Settings
from curie_api.gitflow import trusted_clone_url
from curie_api.models import Agent

_CORPUS = json.loads(
    (Path(__file__).resolve().parents[3] / "tests/vectors/repo-full-name.json").read_text()
)
VALID_REPOSITORIES: list[dict[str, Any]] = _CORPUS["valid"]
INVALID_REPOSITORIES: list[dict[str, Any]] = _CORPUS["invalid"]


@pytest.mark.parametrize("case", VALID_REPOSITORIES, ids=lambda case: case["name"])
def test_repo_full_name_valid_corpus_preserves_orm_assignment(case: dict[str, str]) -> None:
    agent = Agent(name=f"valid-{case['name']}")
    agent.repo_full_name = case["value"]

    assert agent.repo_full_name == case["value"]


@pytest.mark.parametrize("case", INVALID_REPOSITORIES, ids=lambda case: case["name"])
def test_repo_full_name_invalid_corpus_is_refused_on_orm_assignment(
    case: dict[str, str],
) -> None:
    agent = Agent(name=f"invalid-{case['name']}")

    with pytest.raises(ValueError):
        agent.repo_full_name = case["value"]


@pytest.mark.parametrize("case", VALID_REPOSITORIES, ids=lambda case: case["name"])
def test_repo_full_name_valid_corpus_builds_the_expected_clone_url(
    case: dict[str, str],
) -> None:
    settings = Settings(github_clone_base="https://github.example")

    assert trusted_clone_url(case["value"], settings) == (
        f"https://github.example/{case['url_path']}.git"
    )


@pytest.mark.parametrize("case", INVALID_REPOSITORIES, ids=lambda case: case["name"])
def test_repo_full_name_invalid_corpus_never_reaches_a_clone_url(
    case: dict[str, str],
) -> None:
    settings = Settings(github_clone_base="https://github.example")

    with pytest.raises(ValueError):
        trusted_clone_url(case["value"], settings)
