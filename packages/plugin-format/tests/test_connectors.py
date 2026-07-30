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


def test_bundle_with_a_valid_connector_passes(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "connectors:\n  grafana:\n    image: grafana/mcp-grafana:0.17.2\n"
        "    secrets: [GRAFANA_TOKEN]\n",
    )
    assert validate_bundle(str(root)).valid
