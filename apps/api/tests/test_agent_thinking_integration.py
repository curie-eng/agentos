"""Per-agent thinking depth round-trips through the API against real Postgres.

The sibling of `test_agent_model_integration`, deliberately: `thinking` is the
per-agent half of the two-layer operator control ADR-0098 defines, and it is
stored, exposed and updated exactly the way `model` is (#1182). Create-with,
the null default, PATCH-to-set, and PATCH-leaves-unchanged.

The null default carries the meaning here: NULL is not "thinking off", it is
"this agent expresses no opinion", which falls through to the worker's
`CURIE_THINKING` and, if that is unset too, to sending the runner nothing at all.
"""

from typing import Any


def _create_agent(client: Any, auth_headers: dict[str, str], **body: Any) -> dict[str, Any]:
    resp = client.post(
        "/agents",
        json={"name": "thinking-bot", "slack_channel": "CTHINK001", **body},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def test_agent_defaults_to_null_thinking(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers)
    assert agent["thinking"] is None

    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking"] is None


def test_create_with_thinking_persists(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers, thinking="disabled")
    assert agent["thinking"] == "disabled"

    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking"] == "disabled"


def test_patch_sets_thinking(client: Any, auth_headers: dict[str, str], clean_db: None) -> None:
    agent = _create_agent(client, auth_headers)
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"thinking": "enabled:2000"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking"] == "enabled:2000"


def test_patch_without_thinking_leaves_it_unchanged(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # Omitted means "unchanged", the same convention `model` and slack_channel
    # follow -- a PATCH that renames an agent must not silently clear its
    # thinking depth.
    agent = _create_agent(client, auth_headers, thinking="adaptive")
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"slack_channel": "CTHINK002"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking"] == "adaptive"


def test_the_api_stores_the_value_verbatim_and_does_not_validate_it(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # Deliberate: the vocabulary belongs to the runner (`curie_runner.thinking`),
    # not to the persistence layer, so that swapping the harness is not a schema
    # change. The consequence is that a typo is caught at sandbox boot with a
    # message naming the vocabulary, not at write time -- which is the same
    # bargain `model` already makes (the API does not know which model ids a
    # provider accepts either).
    agent = _create_agent(client, auth_headers, thinking="not-a-real-value")
    assert agent["thinking"] == "not-a-real-value"
