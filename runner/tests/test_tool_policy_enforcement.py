"""`toolPolicy` decided at the two interception points, not just parsed.

The lane `plugin_format.tool_policy` was holding open: "No bundle may ship a real
policy until the runtime lane lands." These pin what landing it means.

The load-bearing test is `test_a_denied_tool_is_refused_by_the_hook_not_only_the_callback`.
`can_use_tool` is skipped whenever another permission rule already allows a call
-- a skill's `allowed-tools` frontmatter is such a rule -- so a decision that
lives only there is bypassable, which is #1852. A policy enforced only in the
callback would be a policy a bundle can walk around by declaring its own
permissions, and every other test here would still pass.
"""

from typing import Any

import pytest
from curie_runner.approval import (
    ApprovalGate,
    build_approval_hook,
    build_can_use_tool,
)
from plugin_format import ToolPolicy


def _policy(**collections: list[str]) -> ToolPolicy:
    return ToolPolicy(
        enforcement="curie/mcp-tool-policy@1",
        allow=collections.get("allow", []),
        approvalRequired=collections.get("approval_required", []),
        deny=collections.get("deny", []),
    )


def _gate(policy: ToolPolicy | None, **kwargs: Any) -> ApprovalGate:
    return ApprovalGate(
        tool_policy=policy,
        bundle_name="sre-bot",
        connector_servers={"k8s-write", "self-upgrade"},
        mcp_servers=set(),
        **kwargs,
    )


def _hook_call(gate: ApprovalGate, tool_name: str) -> dict[str, Any]:
    hooks = build_approval_hook(gate)
    matcher = hooks["PreToolUse"][0]
    callback = matcher.hooks[0]
    import anyio

    return anyio.run(
        callback, {"tool_name": tool_name, "tool_input": {}}, None, None
    )


def _denied(result: dict[str, Any]) -> bool:
    decision = (result.get("hookSpecificOutput") or {}).get("permissionDecision")
    return decision == "deny"


def _reason(result: dict[str, Any]) -> str:
    return (result.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


def test_a_denied_tool_is_refused_by_the_hook_not_only_the_callback() -> None:
    """The whole point, and the one that fails if enforcement lives in the
    callback alone: the hook is the only interception no permission rule can
    shadow."""
    gate = _gate(_policy(deny=["k8s-write/*"]))
    result = _hook_call(gate, "mcp__k8s-write__restart_deployment")
    assert _denied(result)
    assert "denied by this agent's tool policy" in _reason(result)
    # A refusal is not an approval request: nothing was recorded for a human.
    assert gate.pending_summary is None


def test_a_refusal_does_not_invite_an_approval() -> None:
    """A policy `deny` and a gate block are both denials on the wire and must not
    read the same to a person: one has an audience who can permit it, the other
    does not."""
    gate = _gate(_policy(deny=["k8s-write/restart_deployment"]))
    _hook_call(gate, "mcp__k8s-write__restart_deployment")
    assert gate.pending_summary is None
    assert gate.pending_route is None
    assert gate.pending_granted_tool is None


def test_an_unclassified_mcp_tool_is_refused() -> None:
    """The documented default, and what actually defends the surface: a tool the
    server begins advertising after the bundle was authored is not inherited."""
    gate = _gate(_policy(allow=["k8s-write/restart_deployment"]))
    assert _denied(_hook_call(gate, "mcp__k8s-write__something_new"))


def test_a_builtin_is_untouched_by_a_policy_about_connectors() -> None:
    """`Bash` cannot be written into a `<server>/<tool>` pattern, so reading "no
    pattern matched" as the deny default would revoke it from every bundle that
    ships a policy."""
    gate = _gate(_policy(deny=["k8s-write/*"]))
    assert _hook_call(gate, "Bash") == {}


def test_an_allowed_tool_falls_through_to_the_existing_gate() -> None:
    """A policy may only ADD restrictions (#520). An `allow` does not hollow out
    an operator gate on the same tool."""
    gate = _gate(
        _policy(allow=["k8s-write/restart_deployment"]),
        required=frozenset({"mcp__k8s-write__restart_deployment"}),
    )
    result = _hook_call(gate, "mcp__k8s-write__restart_deployment")
    assert _denied(result), "an operator-gated tool must stay gated"
    assert gate.pending_summary is not None, "and it must still ask a human"


def test_approval_required_gates_a_tool_the_operator_never_named() -> None:
    gate = _gate(_policy(approval_required=["self-upgrade/upgrade_platform"]))
    result = _hook_call(gate, "mcp__self-upgrade__upgrade_platform")
    assert _denied(result)
    # A block, not a refusal: this one has an audience.
    assert gate.pending_summary is not None
    assert "denied by this agent's tool policy" not in _reason(result)


def test_a_bundle_with_no_policy_is_unchanged() -> None:
    """Every bundle shipped to date. The hook keeps its fast path."""
    gate = _gate(None)
    assert _hook_call(gate, "mcp__k8s-write__restart_deployment") == {}
    assert _hook_call(gate, "Bash") == {}


@pytest.mark.parametrize(
    "tool",
    ["mcp__k8s-write__restart_deployment", "mcp__k8s-write__something_new"],
)
def test_the_callback_agrees_with_the_hook(tool: str) -> None:
    """Both interception points share `_decide_gate`, so they must not disagree
    -- the defect class #1852 closed for the two invocation contexts."""
    import anyio

    gate = _gate(_policy(deny=["k8s-write/restart_deployment"]))
    callback = build_can_use_tool(gate)
    result = anyio.run(callback, tool, {}, None)
    assert type(result).__name__ == "PermissionResultDeny"
