"""Per-agent model selection round-trips through the API against real Postgres.

The `model` column is forwarded as CURIE_MODEL at sandbox boot by the worker
(#254); the API's job is to store, expose, and update it. Create-with-model, the
null default, PATCH-to-set, and PATCH-leaves-unchanged.
"""

from typing import Any


def _create_agent(
    client: Any, auth_headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    resp = client.post(
        "/agents",
        json={"name": "model-bot", "slack_channel": "CMODEL001", **body},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def test_agent_defaults_to_null_model(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers)
    assert agent["model"] is None

    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] is None


def test_create_with_model_persists(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers, model="glm-5.2")
    assert agent["model"] == "glm-5.2"

    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "glm-5.2"


def test_patch_sets_model(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers)
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"model": "kimi-k2.1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "kimi-k2.1"


def test_patch_without_model_leaves_it_unchanged(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent = _create_agent(client, auth_headers, model="deepseek-v4")
    # A PATCH touching only slack_channel must not clear the model.
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"slack_channel": "CMOVED001"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slack_channel"] == "CMOVED001"
    assert body["model"] == "deepseek-v4"


def test_an_empty_model_is_refused_and_the_error_points_at_null(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # #1355: the twin of the thinking case, on the adjacent line. An empty string
    # is NOT an alternate reset -- apply_model_env reads
    # `override if override is not None else config.model`, so "" is not None and
    # wins the ternary, then `if model:` is falsy and CURIE_MODEL is never
    # emitted. The SDK then falls back to its own built-in default instead of the
    # operator's platform model, and on the BYO lane it dials a custom endpoint
    # asking for a built-in Anthropic model id. Refuse it, and say what to send.
    agent = _create_agent(client, auth_headers, model="kimi-k2")
    resp = client.patch(
        f"/agents/{agent['id']}", json={"model": ""}, headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    assert "null" in resp.text, f"the error must point at the real reset: {resp.text}"

    # And the refusal left the stored value alone.
    resp = client.get(f"/agents/{agent['id']}", headers=auth_headers)
    assert resp.json()["model"] == "kimi-k2"

    # Create is refused identically -- same field, same nonsense, same answer.
    resp = client.post(
        "/agents",
        json={"name": "empty-model", "slack_channel": "CMODEL011", "model": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


def test_a_whitespace_only_model_is_refused_on_both_paths(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # Whitespace is worse than empty: it passes the falsy check downstream, so it
    # is stored AND forwarded as a garbage model id the harness cannot resolve.
    # The same predicate that catches "" has to catch it.
    resp = client.post(
        "/agents",
        json={"name": "ws-model", "slack_channel": "CMODEL012", "model": "   "},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text

    agent = _create_agent(client, auth_headers, model="kimi-k2")
    resp = client.patch(
        f"/agents/{agent['id']}", json={"model": "  "}, headers=auth_headers
    )
    assert resp.status_code == 422, resp.text


def test_model_and_thinking_answer_an_empty_string_identically(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The parity assertion itself, not just two tests that happen to agree. #1334
    # repaired one nullable override and left its twin two lines away accepting
    # what the first refused; this fails the moment they diverge again.
    agent = _create_agent(client, auth_headers, model="kimi-k2", thinking="adaptive")
    for field in ("model", "thinking"):
        resp = client.patch(
            f"/agents/{agent['id']}", json={field: ""}, headers=auth_headers
        )
        assert resp.status_code == 422, f"{field} must refuse an empty string: {resp.text}"
        assert "null" in resp.text, f"{field} must point at null: {resp.text}"

    # And both clear to the platform default through the SAME gesture.
    resp = client.patch(
        f"/agents/{agent['id']}",
        json={"model": None, "thinking": None},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] is None
    assert resp.json()["thinking"] is None
