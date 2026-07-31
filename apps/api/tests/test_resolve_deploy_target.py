"""Resolving a named deploy target (ADR-0089, #1166).

The endpoint exists so there is exactly ONE parser for deploy.yaml. A second
implementation in the CLI could disagree with this one about the same file, and
the file's whole job is to be unambiguous about where a deploy lands -- a
disagreement routes the deploy somewhere the author did not intend and reports
success.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A client with NO database fixture, deliberately.

    This endpoint is a pure function of the text posted to it. Building the
    client without the db fixture is the assertion: if resolution ever grew a
    database dependency, this module would stop importing.
    """

    from curie_api.main import create_app

    return TestClient(create_app())


@pytest.fixture(scope="module")
def auth_headers() -> dict[str, str]:
    from curie_api.config import get_settings

    return {"X-API-Key": get_settings().api_key}


REAL = (
    "targets:\n"
    "  dev:\n"
    "    agent: acme-dev\n"
    "    env: dev\n"
    "    slack_channel: C0EXAMPLE2\n"
    "  prod:\n"
    "    agent: acme-bot\n"
    "    env: prod\n"
    "    slack_channel: C0EXAMPLE1\n"
)


def _resolve(client: TestClient, headers: dict, content: str, target: str):
    return client.post(
        "/deploy-targets/resolve", json={"content": content, "target": target}, headers=headers
    )


def test_resolves_the_named_target(client: TestClient, auth_headers: dict) -> None:
    r = _resolve(client, auth_headers, REAL, "prod")
    assert r.status_code == 200, r.text
    assert r.json() == {"agent": "acme-bot", "env": "prod", "slack_channel": "C0EXAMPLE1"}


def test_each_target_resolves_differently(client: TestClient, auth_headers: dict) -> None:
    dev = _resolve(client, auth_headers, REAL, "dev").json()
    prod = _resolve(client, auth_headers, REAL, "prod").json()
    assert dev["agent"] != prod["agent"]
    assert dev["env"] == "dev" and prod["env"] == "prod"


def test_an_unknown_target_404s_and_lists_what_exists(
    client: TestClient, auth_headers: dict
) -> None:
    # Naming a target that is not declared must not fall back to a default --
    # that would deploy somewhere the caller did not ask for.
    r = _resolve(client, auth_headers, REAL, "staging")
    assert r.status_code == 404
    assert "dev, prod" in r.json()["detail"]


def test_a_validation_error_is_returned_not_swallowed(
    client: TestClient, auth_headers: dict
) -> None:
    # Each of these describes a deploy that would otherwise SUCCEED against the
    # wrong agent, environment, or channel.
    r = _resolve(client, auth_headers, "targets:\n  p:\n    env: staging\n", "p")
    assert r.status_code == 400
    assert "deploy.bad_env" in r.json()["detail"]


def test_unparseable_yaml_is_a_400_naming_the_problem(
    client: TestClient, auth_headers: dict
) -> None:
    r = _resolve(client, auth_headers, "targets:\n  p:\n   env: [unclosed\n", "p")
    assert r.status_code == 400
    assert "unparseable" in r.json()["detail"]
