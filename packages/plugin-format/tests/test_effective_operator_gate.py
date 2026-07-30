"""Unit table for the shared operator-gate normalization helpers (#703).

``effective_operator_gate(bundle_name, servers, name)`` maps an operator-supplied
``CURIE_APPROVAL_REQUIRED_TOOLS`` name to the effective runtime tool name the SDK
plugin-prefixes a bundle MCP tool to, returning the rewritten effective name, the
name verbatim when it needs no rewrite (a built-in or an already-effective name),
or ``None`` when it is an unresolvable ``mcp__`` shorthand (the caller fails
closed). ``declared_mcp_server_names(root)`` reads the bundle's declared MCP server
names (inline manifest ``mcpServers`` + root ``.mcp.json``) with the same
poison-to-``None`` semantics as ``validate._validate_mcp``.

The ``mcp__plugin_<bundle>_<server>__`` prefix is the exact shape the deploy
validator asserts (validate.py:
    ``expected_prefixes = {f"mcp__plugin_{manifest.name}_{s}__" for s in mcp_servers}``
), so this helper and the validator normalize identically by construction (#453).
"""

import json

from plugin_format import declared_mcp_server_names, effective_operator_gate

# --- effective_operator_gate: shorthand -> effective / verbatim / None -----------


def test_shorthand_for_declared_server_maps_to_effective_name() -> None:
    assert (
        effective_operator_gate("b", {"github"}, "mcp__github__update_issue")
        == "mcp__plugin_b_github__update_issue"
    )


def test_builtin_name_passes_verbatim() -> None:
    # No mcp__ prefix -> a built-in, armed by raw name; never rewritten, even when
    # the bundle declares servers.
    assert effective_operator_gate("b", {"github"}, "Bash") == "Bash"


def test_already_effective_name_passes_verbatim() -> None:
    # Already mcp__plugin_-prefixed -> passed through unchanged (the suffix is
    # unknowable without running the server, mirroring validate.py).
    name = "mcp__plugin_b_github__update_issue"
    assert effective_operator_gate("b", {"github"}, name) == name


def test_shorthand_for_undeclared_server_is_unresolvable() -> None:
    # mcp__-shaped, names a server the bundle does not declare, not already
    # mcp__plugin_-prefixed -> unresolvable; the caller fails closed.
    assert effective_operator_gate("b", {"github"}, "mcp__slack__post") is None


def test_shorthand_for_server_name_containing_double_underscore() -> None:
    # A declared server name may itself contain "__" (McpConfig permits it, the
    # manifest validator accepts it). The shorthand must resolve by MATCHING the
    # declared server set, not by splitting at the FIRST "__" -- otherwise
    # mcp__foo__bar__do splits to server "foo" (undeclared) and fails a valid
    # bundle closed. The server is foo__bar, the tool is do.
    assert (
        effective_operator_gate("b", {"foo__bar"}, "mcp__foo__bar__do")
        == "mcp__plugin_b_foo__bar__do"
    )


def test_shorthand_prefers_longest_matching_server_name() -> None:
    # When one declared server name is a prefix of another, the LONGEST match wins
    # so the resolution is unambiguous: with both "foo" and "foo__bar" declared,
    # mcp__foo__bar__do resolves to server foo__bar (tool do), not foo (tool
    # bar__do).
    assert (
        effective_operator_gate("b", {"foo", "foo__bar"}, "mcp__foo__bar__do")
        == "mcp__plugin_b_foo__bar__do"
    )


def test_already_prefixed_name_for_undeclared_prefix_fails_closed() -> None:
    # An already mcp__plugin_-prefixed operator name is NOT trusted verbatim: it
    # must match an expected prefix mcp__plugin_<bundle>_<server>__ for a DECLARED
    # server (with a non-empty tool remainder), mirroring validate.py's
    # expected_prefixes check. A typo'd wrongbundle/wrongserver name arms a literal
    # the runtime never matches -> fail OPEN; this must fail CLOSED (None).
    assert (
        effective_operator_gate("b", {"github"}, "mcp__plugin_wrongbundle_wrongserver__tool")
        is None
    )


def test_already_prefixed_name_with_empty_tool_suffix_fails_closed() -> None:
    # The bare expected prefix with no tool suffix arms nothing -> fail closed.
    assert effective_operator_gate("b", {"github"}, "mcp__plugin_b_github__") is None


def test_shorthand_with_no_declared_servers_is_unresolvable() -> None:
    assert effective_operator_gate("b", set(), "mcp__github__update_issue") is None


def test_poisoned_servers_fail_mcp_names_closed_but_pass_builtins_verbatim() -> None:
    # servers=None is the poison from an unreadable MCP declaration: neither an
    # mcp__ shorthand NOR an already mcp__plugin_-prefixed name can be verified
    # against the declared-server set, so both fail CLOSED (None). This runtime
    # cross-check is the sole defense for the never-deploy-validated operator
    # override, so "cannot verify" must fail closed, not pass through. Only a
    # built-in (no mcp__ prefix, no server lookup) still passes verbatim.
    assert effective_operator_gate("b", None, "mcp__github__update_issue") is None
    assert effective_operator_gate("b", None, "mcp__plugin_b_github__update_issue") is None
    assert effective_operator_gate("b", None, "Bash") == "Bash"


def test_falsy_bundle_name_fails_mcp_names_closed() -> None:
    # No bundle name means the effective prefix cannot be constructed to verify an
    # mcp__ name -> fail closed. Built-ins still pass verbatim.
    assert effective_operator_gate("", {"github"}, "mcp__github__update_issue") is None
    assert effective_operator_gate("", {"github"}, "mcp__plugin_b_github__update_issue") is None
    assert effective_operator_gate("", {"github"}, "Bash") == "Bash"


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
