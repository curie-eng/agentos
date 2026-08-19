"""Declared-connector validation (ADR-0086, #1063).

Every case here is one an author would otherwise discover as an opaque
Kubernetes apply failure -- or worse, as a connector that comes up and quietly
does the wrong thing. Catching them at validation is the point of the file
existing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plugin_format import validate_bundle
from plugin_format.connectors import validate_connectors


def _bundle(root: Path, connectors_yaml: str | None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "b", "version": "0.1.0", "description": "t"}), encoding="utf-8"
    )
    (root / "skills" / "b").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "b" / "SKILL.md").write_text(
        "---\nname: b\ndescription: t\n---\nhi\n", encoding="utf-8"
    )
    if connectors_yaml is not None:
        (root / "connectors.yaml").write_text(connectors_yaml, encoding="utf-8")
    return root


def _codes(data: object) -> list[str]:
    _, errors = validate_connectors(data)
    return [c for c, _ in errors]


# --------------------------------------------------------------------------- #
# Accepted shapes
# --------------------------------------------------------------------------- #
def test_hosted_connector_is_accepted() -> None:
    parsed, errors = validate_connectors(
        {
            "connectors": {
                "grafana": {
                    "image": "grafana/mcp-grafana:0.17.2",
                    "args": ["-t", "streamable-http"],
                    "env": {"GRAFANA_URL": "https://g.example.com"},
                    "secrets": ["GRAFANA_TOKEN"],
                }
            }
        }
    )
    assert errors == []
    assert parsed is not None
    assert parsed.connectors["grafana"].is_hosted


def test_remote_connector_is_accepted() -> None:
    parsed, errors = validate_connectors(
        {"connectors": {"internal": {"url": "https://mcp.internal/mcp", "secrets": ["T"]}}}
    )
    assert errors == []
    assert parsed is not None
    assert not parsed.connectors["internal"].is_hosted


def test_absent_file_is_fine(tmp_path: Path) -> None:
    # A bundle with no hosted connectors simply omits the file.
    assert validate_bundle(str(_bundle(tmp_path, None))).valid


# --------------------------------------------------------------------------- #
# Rejected shapes -- each would otherwise fail late and obscurely
# --------------------------------------------------------------------------- #
def test_both_image_and_url_is_ambiguous() -> None:
    # Who owns the process? Guessing either way silently ignores half the spec.
    assert "connectors.ambiguous" in _codes(
        {"connectors": {"g": {"image": "x:1", "url": "https://y/mcp"}}}
    )


def test_neither_image_nor_url_is_underspecified() -> None:
    assert "connectors.underspecified" in _codes({"connectors": {"g": {"secrets": ["T"]}}})


def test_runtime_config_on_a_remote_connector_is_rejected() -> None:
    # args/env configure a process Curie starts. On a url connector they would
    # be accepted and then silently do nothing -- the worst kind of no-op.
    assert "connectors.remote_has_runtime" in _codes(
        {"connectors": {"g": {"url": "https://y/mcp", "env": {"A": "b"}}}}
    )


def test_headers_on_a_hosted_connector_are_rejected() -> None:
    assert "connectors.hosted_has_headers" in _codes(
        {"connectors": {"g": {"image": "x:1", "headers": {"Authorization": "Bearer x"}}}}
    )


@pytest.mark.parametrize(
    "name",
    [
        "Grafana",  # uppercase -- not a DNS label
        "grafana_mcp",  # underscore -- not a DNS label
        "-grafana",  # leading dash
        "grafana-",  # trailing dash
        "g" * 41,  # over the cap
        "",  # empty
    ],
)
def test_name_must_be_a_dns_label(name: str) -> None:
    # The name becomes a Kubernetes object name and a Service DNS label. A bad
    # one fails at apply time with a message about the object, not the bundle.
    assert "connectors.bad_name" in _codes({"connectors": {name: {"image": "x:1"}}})


def test_unknown_key_is_rejected_not_ignored() -> None:
    # This package is lenient elsewhere because real Claude Code bundles carry
    # keys it does not model. connectors.yaml is Curie's own file with no
    # external producer, so an unrecognised key is a typo -- and `secretz`
    # silently ignored means a connector that starts without its credential.
    assert _codes({"connectors": {"g": {"image": "x:1", "secretz": ["T"]}}}) != []


def test_port_out_of_range_is_rejected() -> None:
    assert "connectors.bad_port" in _codes({"connectors": {"g": {"image": "x:1", "port": 99999}}})


def test_non_mapping_file_is_rejected() -> None:
    assert "connectors.not_object" in _codes(["not", "a", "mapping"])


# --------------------------------------------------------------------------- #
# Reaching it through validate_bundle
# --------------------------------------------------------------------------- #
def test_bundle_surfaces_a_connector_error(tmp_path: Path) -> None:
    root = _bundle(tmp_path, "connectors:\n  Bad_Name:\n    image: x:1\n")
    result = validate_bundle(str(root))
    assert not result.valid
    assert any(e.code == "connectors.bad_name" for e in result.errors)


def test_bundle_surfaces_unparseable_yaml(tmp_path: Path) -> None:
    root = _bundle(tmp_path, "connectors:\n  g:\n   image: [unclosed\n")
    result = validate_bundle(str(root))
    assert not result.valid
    assert any(e.code == "connectors.unreadable" for e in result.errors)


def test_bundle_rejects_a_duplicate_connector_name(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "connectors:\n"
        "  grafana:\n"
        "    url: https://first.example.com/mcp\n"
        "  grafana:\n"
        "    url: https://second.example.com/mcp\n",
    )
    result = validate_bundle(str(root))
    assert not result.valid
    issue = next(e for e in result.errors if e.code == "connectors.duplicate_connector")
    assert "grafana" in issue.message


def test_bundle_with_a_valid_connector_passes(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "connectors:\n  grafana:\n    image: grafana/mcp-grafana:0.17.2\n"
        "    secrets: [GRAFANA_TOKEN]\n",
    )
    assert validate_bundle(str(root)).valid


# --------------------------------------------------------------------------- #
# One name, one owner (#1118)
# --------------------------------------------------------------------------- #
def test_a_name_in_both_connectors_yaml_and_mcp_json_is_rejected(tmp_path: Path) -> None:
    # Curie injects the connector's entry alongside whatever the bundle declares.
    # With both naming `grafana`, which one the agent talks to is decided
    # downstream and the loser is overridden with no diagnostic -- either the
    # author's committed entry is ignored, or it silently wins over the objects
    # Curie actually created. Caught here, the fix is a one-line edit.
    root = _bundle(tmp_path, "connectors:\n  grafana:\n    image: x:1\n")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"grafana": {"type": "http", "url": "http://hand-written/mcp"}}}),
        encoding="utf-8",
    )
    result = validate_bundle(str(root))
    assert not result.valid
    assert any(e.code == "connectors.duplicate_server" for e in result.errors)


def test_distinct_names_across_the_two_files_are_fine(tmp_path: Path) -> None:
    # The files are complementary by design: connectors.yaml for what Curie
    # hosts, .mcp.json for anything else (a stdio server, say).
    root = _bundle(tmp_path, "connectors:\n  grafana:\n    image: x:1\n")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"local-tool": {"command": "./bin/tool"}}}),
        encoding="utf-8",
    )
    assert validate_bundle(str(root)).valid


def test_an_unreadable_mcp_config_does_not_add_a_confusing_second_error(tmp_path: Path) -> None:
    # `_validate_mcp` already errors on the unreadable declaration. Cross-checking
    # a partial set would name a collision we cannot actually confirm.
    root = _bundle(tmp_path, "connectors:\n  grafana:\n    image: x:1\n")
    (root / ".mcp.json").write_text("{not json", encoding="utf-8")
    result = validate_bundle(str(root))
    assert not result.valid
    assert not any(e.code == "connectors.duplicate_server" for e in result.errors)


def test_a_typo_in_a_placeholder_is_rejected() -> None:
    # Unsubstituted text reaches the container verbatim, so the connector starts
    # and rejects every call. Nothing catches that at runtime.
    codes = _codes({"connectors": {"g": {"image": "x:1", "args": ["-h", "${CURIE_ALOWED_HOSTS}"]}}})
    assert "connectors.unknown_placeholder" in codes


def test_known_placeholders_are_accepted_in_args_and_env() -> None:
    _, errors = validate_connectors(
        {
            "connectors": {
                "g": {
                    "image": "x:1",
                    "args": ["-h", "${CURIE_ALLOWED_HOSTS}", "-p", "${CURIE_CONNECTOR_PORT}"],
                    "env": {"SELF": "${CURIE_CONNECTOR_URL}", "H": "${CURIE_CONNECTOR_HOST}"},
                }
            }
        }
    )
    assert errors == []


def test_the_validator_and_the_renderer_share_one_placeholder_list() -> None:
    # Two lists would drift: the renderer would substitute something the
    # validator rejects, or accept something that reaches the container raw.
    from plugin_format.connector_render import PLACEHOLDERS

    for name in PLACEHOLDERS:
        _, errors = validate_connectors(
            {"connectors": {"g": {"image": "x:1", "args": ["${" + name + "}"]}}}
        )
        assert errors == [], f"renderer substitutes ${{{name}}} but the validator rejects it"


# --------------------------------------------------------------------------- #
# The hosted form's escape hatch for tiers that cannot host -- #1160
# --------------------------------------------------------------------------- #
def test_a_hosted_connector_may_declare_where_to_reach_it_when_unhosted() -> None:
    parsed, errors = validate_connectors(
        {
            "connectors": {
                "grafana": {
                    "image": "grafana/mcp-grafana:0.17.2",
                    "unhosted_url": "${GRAFANA_MCP_URL}",
                }
            }
        }
    )
    assert errors == []
    assert parsed is not None
    assert parsed.connectors["grafana"].is_hosted, "still hosted where Curie can host"


def test_unhosted_url_on_a_remote_connector_is_rejected() -> None:
    # A `url` connector is already reachable on every tier, so the fallback
    # could never apply -- accepting it would be a silent no-op.
    assert "connectors.remote_has_unhosted_url" in _codes(
        {"connectors": {"g": {"url": "https://y/mcp", "unhosted_url": "http://z/mcp"}}}
    )


# --------------------------------------------------------------------------- #
# Referencing a Secret Curie did not create -- #1163
# --------------------------------------------------------------------------- #
def test_a_connector_may_reference_a_secret_provisioned_out_of_band() -> None:
    parsed, errors = validate_connectors(
        {
            "connectors": {
                "grafana": {
                    "image": "x:1",
                    "secrets": [{"name": "TOKEN", "from_secret": "grafana-mcp"}],
                }
            }
        }
    )
    assert errors == []
    assert parsed is not None
    spec = parsed.connectors["grafana"]
    assert spec.secret_names() == ["TOKEN"]
    # The property the whole change exists for: nothing in the deploy path
    # needs to hold this credential, which is what lets a reconciler apply a
    # connector without holding every agent's secrets (ADR-0090).
    assert spec.resolved_secrets() == []


def test_the_literal_form_still_needs_resolving() -> None:
    parsed, _ = validate_connectors({"connectors": {"g": {"image": "x:1", "secrets": ["TOKEN"]}}})
    assert parsed is not None
    assert parsed.connectors["g"].resolved_secrets() == ["TOKEN"]


def test_both_forms_can_be_mixed_on_one_connector() -> None:
    parsed, errors = validate_connectors(
        {
            "connectors": {
                "g": {"image": "x:1", "secrets": ["OWNED", {"name": "REFD", "from_secret": "s"}]}
            }
        }
    )
    assert errors == []
    assert parsed is not None
    assert parsed.connectors["g"].secret_names() == ["OWNED", "REFD"]
    assert parsed.connectors["g"].resolved_secrets() == ["OWNED"]


def test_an_empty_from_secret_is_rejected() -> None:
    # It renders a secretKeyRef at a Secret named '', which the API server
    # rejects at APPLY -- long after the deploy looked like it worked.
    assert "connectors.empty_secret_ref" in _codes(
        {"connectors": {"g": {"image": "x:1", "secrets": [{"name": "T", "from_secret": ""}]}}}
    )


def test_the_same_env_name_declared_twice_is_rejected() -> None:
    # Two env entries with one name means the container silently gets whichever
    # the renderer emitted last -- possibly pointed at the wrong Secret.
    assert "connectors.duplicate_secret" in _codes(
        {"connectors": {"g": {"image": "x:1", "secrets": ["T", {"name": "T", "from_secret": "s"}]}}}
    )


def test_key_defaults_to_the_env_var_name() -> None:
    parsed, _ = validate_connectors(
        {"connectors": {"g": {"image": "x:1", "secrets": [{"name": "T", "from_secret": "s"}]}}}
    )
    assert parsed is not None
    assert parsed.connectors["g"].secrets[0].secret_key() == "T"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Names Curie's own platform MCP servers occupy -- #1200
# --------------------------------------------------------------------------- #
RESERVED_NAMES = ["curie", "curie-state"]


@pytest.mark.parametrize("name", RESERVED_NAMES)
def test_a_reserved_platform_server_name_is_rejected(name: str) -> None:
    # `curie` is the approval server and `curie-state` the durable state server.
    # Both ride the same mcp_servers map a declared connector rides, so a
    # connector claiming the name replaces the platform server in the agent's
    # session -- the agent quietly loses request_approval or the state tools,
    # with nothing logged and nothing failing until a skill calls one.
    assert "connectors.reserved_name" in _codes({"connectors": {name: {"image": "x:1"}}})


@pytest.mark.parametrize("name", RESERVED_NAMES)
def test_bundle_rejects_a_reserved_connector_name(tmp_path: Path, name: str) -> None:
    # The shape the issue reports as validating clean today: connectors.yaml
    # declares the name, no .mcp.json exists, so the #1118 cross-check has
    # nothing to compare against and the bundle sails through deploy.
    root = _bundle(tmp_path, f"connectors:\n  {name}:\n    image: x:1\n")
    result = validate_bundle(str(root))
    assert not result.valid
    offending = [e for e in result.errors if e.code == "connectors.reserved_name"]
    assert offending, [e.code for e in result.errors]
    assert name in offending[0].message


def test_a_name_merely_starting_with_curie_is_accepted() -> None:
    # The fence is the two exact names, not the `curie-` prefix. Fencing the
    # prefix would reject a legitimate `curie-docs` connector, which is a worse
    # accidental collision than the one being prevented.
    assert _codes({"connectors": {"curie-docs": {"image": "x:1"}}}) == []


def test_a_reserved_name_and_a_bad_shape_report_both() -> None:
    # Every other check in this loop accumulates, so the author sees the whole
    # file's problems in one pass rather than one rename per deploy attempt.
    codes = _codes({"connectors": {"curie": {"secrets": ["T"]}}})
    assert "connectors.reserved_name" in codes
    assert "connectors.underspecified" in codes
