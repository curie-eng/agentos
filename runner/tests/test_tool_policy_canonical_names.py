"""Live SDK tool name -> the canonical `"<server>/<tool>"` a policy speaks.

`plugin_format.tool_policy` states the split of work outright: "Mapping canonical
-> live SDK name is the runtime lane's job and is out of scope here." This is
that mapping run backwards, and these tests pin the two things that make it more
than a string split.

**Underscores.** Neither mount shape can be parsed by splitting on `__`, because
a server name may contain them. The known-server sets are what disambiguate, and
a test that only ever uses hyphenated names would pass with a naive split.

**The refusal.** `None` for an `mcp__` name is not "ungoverned", it is "could not
attribute this to a declared server". Collapsing that with "not an MCP tool" is
the fail-open the unclassified-is-denied default exists to prevent, which is why
`is_mcp_tool` is asked separately.
"""

from curie_runner.approval import canonical_tool_name, is_mcp_tool


def _canonical(live: str, **kwargs: object) -> str | None:
    defaults: dict[str, object] = {
        "bundle_name": "sre-bot",
        "mcp_servers": set(),
        "connector_servers": set(),
    }
    defaults.update(kwargs)
    return canonical_tool_name(live, **defaults)  # type: ignore[arg-type]


def test_a_builtin_is_not_an_mcp_tool() -> None:
    # A policy pattern is `<server>/<tool>`, so `Bash` cannot appear in one and
    # is not governed. Answering this separately is what stops a policy about
    # connectors from revoking every built-in.
    assert not is_mcp_tool("Bash")
    assert not is_mcp_tool("Read")
    assert is_mcp_tool("mcp__k8s-write__restart_deployment")


def test_a_connector_tool_maps_to_its_bare_server() -> None:
    assert (
        _canonical(
            "mcp__k8s-write__restart_deployment",
            connector_servers={"k8s-write"},
        )
        == "k8s-write/restart_deployment"
    )


def test_a_plugin_mounted_tool_carries_the_bundle_infix() -> None:
    assert (
        _canonical("mcp__plugin_sre-bot_probe__ping", mcp_servers={"probe"})
        == "probe/ping"
    )


def test_an_underscored_server_name_still_resolves() -> None:
    """The case a naive split on `__` gets wrong, in both shapes."""
    assert (
        _canonical("mcp__k8s_write__restart_deployment", connector_servers={"k8s_write"})
        == "k8s_write/restart_deployment"
    )
    assert (
        _canonical("mcp__plugin_sre-bot_my_probe__ping", mcp_servers={"my_probe"})
        == "my_probe/ping"
    )


def test_an_underscored_tool_name_survives_intact() -> None:
    assert (
        _canonical(
            "mcp__self-upgrade__upgrade_platform",
            connector_servers={"self-upgrade"},
        )
        == "self-upgrade/upgrade_platform"
    )


def test_an_undeclared_server_is_a_refusal_not_a_pass() -> None:
    """The drift the unclassified default exists to catch.

    A connector image that starts advertising a tool from a server this bundle
    never declared must not read as "no policy applies to it".
    """
    assert _canonical("mcp__rogue__delete_everything") is None
    assert is_mcp_tool("mcp__rogue__delete_everything")


def test_an_unknowable_server_set_refuses_rather_than_guessing() -> None:
    """`None` is the `declared_mcp_server_names` poison: unknowable, not empty."""
    assert _canonical("mcp__probe__ping", mcp_servers=None) is None
    assert _canonical("mcp__probe__ping", connector_servers=None) is None


def test_a_connector_wins_a_name_readable_as_either_shape() -> None:
    """Deterministic, and stated in the helper: the bare form is preferred.

    A connector literally named `plugin_<bundle>_<server>` produces a live name
    that also parses as a plugin mount. Something has to decide, and silently
    depending on set iteration order would make the answer vary per process.
    """
    live = "mcp__plugin_sre-bot_probe__ping"
    assert (
        _canonical(
            live,
            connector_servers={"plugin_sre-bot_probe"},
            mcp_servers={"probe"},
        )
        == "plugin_sre-bot_probe/ping"
    )


def test_a_prefix_with_no_tool_left_is_a_refusal() -> None:
    assert _canonical("mcp__k8s-write__", connector_servers={"k8s-write"}) is None


def test_no_bundle_name_cannot_resolve_a_plugin_mount() -> None:
    """The plugin prefix is built from the bundle name; without one there is no
    prefix to match, and guessing would attribute a tool to a server on the
    strength of a substring."""
    assert _canonical("mcp__plugin_sre-bot_probe__ping", bundle_name=None, mcp_servers={"probe"}) is None
