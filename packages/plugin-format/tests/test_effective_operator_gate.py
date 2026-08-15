"""Unit table for the shared operator-gate normalization helpers (#703).

``effective_operator_gate(bundle_name, servers, name, connector_servers=...)`` maps
an operator-supplied
``CURIE_APPROVAL_REQUIRED_TOOLS`` name to the effective runtime tool name the SDK
plugin-prefixes a bundle MCP tool to, returning the rewritten effective name, the
name verbatim when it needs no rewrite (a built-in, an already-effective name, or a
``connectors.yaml`` connector tool), or ``None`` when it is an unresolvable
``mcp__`` shorthand (the caller fails
closed). ``declared_mcp_server_names(root)`` reads the bundle's declared MCP server
names (inline manifest ``mcpServers`` + root ``.mcp.json``) with the same
poison-to-``None`` semantics as ``validate._validate_mcp``;
``connector_server_names(root)`` reads the ``connectors.yaml`` half with the same
semantics (#1495).

The ``mcp__plugin_<bundle>_<server>__`` prefix is the exact shape the deploy
validator asserts (validate.py:
    ``expected_prefixes = {f"mcp__plugin_{manifest.name}_{s}__" for s in mcp_servers}``
), so this helper and the validator normalize identically by construction (#453).
The connector half asserts the OTHER prefix, the bare ``mcp__<connector>__``, since
the runner mounts a connector on ``ClaudeAgentOptions.mcp_servers`` instead of
loading it as a plugin.
"""

import json

from plugin_format import (
    connector_server_names,
    declared_mcp_server_names,
    effective_operator_gate,
)

# --- effective_operator_gate: shorthand -> effective / verbatim / None -----------


def test_shorthand_for_declared_server_maps_to_effective_name() -> None:
    assert (
        effective_operator_gate(
            "b", {"github"}, "mcp__github__update_issue", connector_servers=set()
        )
        == "mcp__plugin_b_github__update_issue"
    )


def test_builtin_name_passes_verbatim() -> None:
    # No mcp__ prefix -> a built-in, armed by raw name; never rewritten, even when
    # the bundle declares servers.
    assert effective_operator_gate("b", {"github"}, "Bash", connector_servers=set()) == "Bash"


def test_already_effective_name_passes_verbatim() -> None:
    # Already mcp__plugin_-prefixed -> passed through unchanged (the suffix is
    # unknowable without running the server, mirroring validate.py).
    name = "mcp__plugin_b_github__update_issue"
    assert effective_operator_gate("b", {"github"}, name, connector_servers=set()) == name


def test_shorthand_for_undeclared_server_is_unresolvable() -> None:
    # mcp__-shaped, names a server the bundle does not declare, not already
    # mcp__plugin_-prefixed -> unresolvable; the caller fails closed.
    assert (
        effective_operator_gate("b", {"github"}, "mcp__slack__post", connector_servers=set())
        is None
    )


def test_shorthand_for_server_name_containing_double_underscore() -> None:
    # A declared server name may itself contain "__" (McpConfig permits it, the
    # manifest validator accepts it). The shorthand must resolve by MATCHING the
    # declared server set, not by splitting at the FIRST "__" -- otherwise
    # mcp__foo__bar__do splits to server "foo" (undeclared) and fails a valid
    # bundle closed. The server is foo__bar, the tool is do.
    assert (
        effective_operator_gate("b", {"foo__bar"}, "mcp__foo__bar__do", connector_servers=set())
        == "mcp__plugin_b_foo__bar__do"
    )


def test_shorthand_prefers_longest_matching_server_name() -> None:
    # When one declared server name is a prefix of another, the LONGEST match wins
    # so the resolution is unambiguous: with both "foo" and "foo__bar" declared,
    # mcp__foo__bar__do resolves to server foo__bar (tool do), not foo (tool
    # bar__do).
    assert (
        effective_operator_gate(
            "b", {"foo", "foo__bar"}, "mcp__foo__bar__do", connector_servers=set()
        )
        == "mcp__plugin_b_foo__bar__do"
    )


def test_already_prefixed_name_for_undeclared_prefix_fails_closed() -> None:
    # An already mcp__plugin_-prefixed operator name is NOT trusted verbatim: it
    # must match an expected prefix mcp__plugin_<bundle>_<server>__ for a DECLARED
    # server (with a non-empty tool remainder), mirroring validate.py's
    # expected_prefixes check. A typo'd wrongbundle/wrongserver name arms a literal
    # the runtime never matches -> fail OPEN; this must fail CLOSED (None).
    assert (
        effective_operator_gate(
            "b", {"github"}, "mcp__plugin_wrongbundle_wrongserver__tool", connector_servers=set()
        )
        is None
    )


def test_already_prefixed_name_with_empty_tool_suffix_fails_closed() -> None:
    # The bare expected prefix with no tool suffix arms nothing -> fail closed.
    assert (
        effective_operator_gate("b", {"github"}, "mcp__plugin_b_github__", connector_servers=set())
        is None
    )


def test_shorthand_with_no_declared_servers_is_unresolvable() -> None:
    assert (
        effective_operator_gate("b", set(), "mcp__github__update_issue", connector_servers=set())
        is None
    )


def test_poisoned_servers_fail_mcp_names_closed_but_pass_builtins_verbatim() -> None:
    # servers=None is the poison from an unreadable MCP declaration: neither an
    # mcp__ shorthand NOR an already mcp__plugin_-prefixed name can be verified
    # against the declared-server set, so both fail CLOSED (None). This runtime
    # cross-check is the sole defense for the never-deploy-validated operator
    # override, so "cannot verify" must fail closed, not pass through. Only a
    # built-in (no mcp__ prefix, no server lookup) still passes verbatim.
    assert (
        effective_operator_gate("b", None, "mcp__github__update_issue", connector_servers=set())
        is None
    )
    assert (
        effective_operator_gate(
            "b", None, "mcp__plugin_b_github__update_issue", connector_servers=set()
        )
        is None
    )
    assert effective_operator_gate("b", None, "Bash", connector_servers=set()) == "Bash"


def test_falsy_bundle_name_fails_mcp_names_closed() -> None:
    # No bundle name means the effective prefix cannot be constructed to verify an
    # mcp__ name -> fail closed. Built-ins still pass verbatim.
    assert (
        effective_operator_gate(
            "", {"github"}, "mcp__github__update_issue", connector_servers=set()
        )
        is None
    )
    assert (
        effective_operator_gate(
            "", {"github"}, "mcp__plugin_b_github__update_issue", connector_servers=set()
        )
        is None
    )
    assert effective_operator_gate("", {"github"}, "Bash", connector_servers=set()) == "Bash"


# --- declared_mcp_server_names: inline manifest + root .mcp.json, poison -> None --


def _bundle(tmp_path, manifest: str, mcp_json: str | None = None):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    if mcp_json is not None:
        (tmp_path / ".mcp.json").write_text(mcp_json, encoding="utf-8")
    return tmp_path


def test_declared_names_union_inline_manifest_and_root_mcp_json(tmp_path) -> None:
    # Both the inline manifest mcpServers object AND a root .mcp.json contribute;
    # the result is their union.
    root = _bundle(
        tmp_path,
        json.dumps({"name": "b", "mcpServers": {"github": {"command": "gh"}}}),
        json.dumps({"mcpServers": {"slack": {"command": "slack-server"}}}),
    )
    assert declared_mcp_server_names(root) == {"github", "slack"}


def test_declared_names_poison_to_none_on_unreadable_declaration(tmp_path) -> None:
    # The string-pointer mcpServers form is a declaration the real loader ignores;
    # _validate_mcp poisons the set to None. This helper must match, so a gate
    # cross-check stays silent rather than asserting against a partial set.
    root = _bundle(
        tmp_path,
        json.dumps({"name": "b", "mcpServers": "config/servers.json"}),
    )
    assert declared_mcp_server_names(root) is None


def test_declared_names_poison_to_none_on_non_utf8_mcp_json(tmp_path) -> None:
    # A non-UTF-8 .mcp.json raises UnicodeDecodeError (a ValueError, NOT an
    # OSError), which must be caught and poison the set to None rather than escape
    # as a bare traceback and defeat the fail-closed intent.
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "b"}), encoding="utf-8"
    )
    (tmp_path / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    assert declared_mcp_server_names(tmp_path) is None


def test_declared_names_poison_to_none_on_non_utf8_manifest(tmp_path) -> None:
    # A non-UTF-8 manifest likewise raises UnicodeDecodeError; poison to None.
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_bytes(b"\xff\xfe\x00")
    assert declared_mcp_server_names(tmp_path) is None


# --- connectors.yaml servers: the OTHER namespacing rule (#1495) -----------------
#
# A connector is mounted straight onto ClaudeAgentOptions.mcp_servers, so its live
# tool name is the bare mcp__<connector>__<tool> -- the same shape Curie's own
# `curie` approval server gets, and NOT the mcp__plugin_<bundle>_<server>__<tool>
# a plugin-loaded server gets. Rewriting a connector gate to the plugin form would
# arm a literal the runtime never produces: green at deploy, silently never fires.


def test_connector_shorthand_passes_verbatim_not_plugin_prefixed() -> None:
    # The load-bearing case: already the live name, so it is returned UNCHANGED.
    name = "mcp__kubernetes-admin__resources_create_or_update"
    assert effective_operator_gate("b", set(), name, connector_servers={"kubernetes-admin"}) == name


def test_connector_gate_is_not_rewritten_even_when_bundle_declares_mcp_servers() -> None:
    # A bundle with both surfaces must namespace each by its own rule: the plugin
    # server is rewritten, the connector is not.
    assert (
        effective_operator_gate(
            "b", {"github"}, "mcp__github__update_issue", connector_servers={"grafana"}
        )
        == "mcp__plugin_b_github__update_issue"
    )
    assert (
        effective_operator_gate(
            "b", {"github"}, "mcp__grafana__query", connector_servers={"grafana"}
        )
        == "mcp__grafana__query"
    )


def test_undeclared_connector_still_fails_closed() -> None:
    assert (
        effective_operator_gate("b", set(), "mcp__ghost__x", connector_servers={"grafana"}) is None
    )


def test_connector_shorthand_with_empty_tool_remainder_fails_closed() -> None:
    # The bare prefix arms nothing, exactly as for the plugin form.
    assert (
        effective_operator_gate("b", set(), "mcp__grafana__", connector_servers={"grafana"}) is None
    )


def test_poisoned_connector_set_fails_mcp_names_closed() -> None:
    # connector_servers=None is the unreadable-connectors.yaml poison: an mcp__
    # name cannot be verified against the full accepted set, so it fails CLOSED
    # even when it would have matched a declared plugin server. Built-ins still
    # pass verbatim.
    assert (
        effective_operator_gate(
            "b", {"github"}, "mcp__github__update_issue", connector_servers=None
        )
        is None
    )
    assert effective_operator_gate("b", {"github"}, "Bash", connector_servers=None) == "Bash"


def test_connector_named_plugin_is_matched_before_the_plugin_branch() -> None:
    # `plugin` is a legal RFC 1123 connector name, so mcp__plugin__<tool> is a
    # legitimate connector tool name. It must not fall into the mcp__plugin_
    # branch and be rejected.
    assert (
        effective_operator_gate("b", set(), "mcp__plugin__do", connector_servers={"plugin"})
        == "mcp__plugin__do"
    )


# --- connector_server_names: read connectors.yaml, poison to None ----------------


def _connectors(tmp_path, text: str):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "b"}), encoding="utf-8"
    )
    (tmp_path / "connectors.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_connector_names_read_from_connectors_yaml(tmp_path) -> None:
    root = _connectors(
        tmp_path,
        "connectors:\n"
        "  kubernetes-admin:\n"
        "    image: ghcr.io/example/k8s-mcp:1.0.0\n"
        "  grafana:\n"
        "    url: https://mcp.internal/mcp\n",
    )
    assert connector_server_names(root) == {"kubernetes-admin", "grafana"}


def test_connector_names_empty_when_no_connectors_yaml(tmp_path) -> None:
    # The distinct fact "read and named nothing", not the unknowable poison: a
    # bundle with no connectors must still have its plugin-server gates checked.
    assert connector_server_names(tmp_path) == set()


def test_connector_names_poison_to_none_on_unparseable_yaml(tmp_path) -> None:
    root = _connectors(tmp_path, "connectors: [oops\n")
    assert connector_server_names(root) is None


def test_connector_names_poison_to_none_on_invalid_connectors(tmp_path) -> None:
    # A connector that does not validate is never mounted, so its name is not a
    # tool surface a gate may be namespaced to: unknowable, not empty.
    root = _connectors(tmp_path, "connectors:\n  Bad_Name:\n    url: https://x/mcp\n")
    assert connector_server_names(root) is None
