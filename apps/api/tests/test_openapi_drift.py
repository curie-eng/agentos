"""The committed OpenAPI document must match the live app."""

import json

from curie_api.export_openapi import openapi_path, render_openapi


def test_committed_openapi_is_current() -> None:
    committed = openapi_path().read_text(encoding="utf-8")
    assert render_openapi() == committed, (
        "apps/api/openapi.json is stale; run "
        "`uv run python -m curie_api.export_openapi` and commit"
    )


def test_approval_reply_placeholder_schema_allows_null_and_string() -> None:
    schemas = json.loads(render_openapi())["components"]["schemas"]

    for name in ("ApprovalRequest", "ApprovalOut"):
        property_schema = schemas[name]["properties"]["reply_placeholder"]
        assert {option["type"] for option in property_schema["anyOf"]} == {
            "null",
            "string",
        }
