"""Boot refuses a gate the bundle's own skill permissions would bypass (#1852).

The defect: a skill's ``allowed-tools`` frontmatter becomes a Claude Code
permission rule, and a permission rule is applied BEFORE the SDK consults
``can_use_tool``. Curie printed the gate as armed while the tool executed with no
approval record.

Enforcement lives at session boot rather than at deploy time because only boot
sees BOTH halves of the gated-tool union: the bundle manifest's ``approvalPolicy``
and the operator's ``CURIE_APPROVAL_REQUIRED_TOOLS``. A deploy-time validator
never re-runs when an operator arms a gate afterwards, which is the case the
issue's acceptance criteria call out by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from curie_runner.approval import (
    ApprovalGate,
    ApprovalPolicyError,
    ApprovalPolicyResolution,
    _entry_tool,
    _whole_tool_allowed,
    assert_gates_not_shadowed,
    build_approval_gate,
    describe_shadowed_gates,
    shadowed_gates,
)


def _bundle(
    root: Path,
    *,
    skills: dict[str, list[str] | None],
    gates: list[str],
    mcp: dict[str, dict[str, object]] | None = None,
) -> str:
    """Write a bundle whose skills, approvalPolicy, and MCP servers are under test.

    Args:
        root: Directory to build in.
        skills: Skill name to its ``allowed-tools`` list, or None to omit the key.
        gates: Tool names the manifest's approvalPolicy gates.
        mcp: Optional ``.mcp.json`` server map.

    Returns:
        The bundle root as a string path.
    """
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "0.1.0",
                "description": "A bundle for the shadowing tests.",
                "approvalPolicy": {"gates": [{"gate": g, "route": "ops"} for g in gates]},
            }
        ),
        encoding="utf-8",
    )
    if mcp is not None:
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    for name, allowed in skills.items():
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"name: {name}", "description: A skill."]
        if allowed is not None:
            lines.append("allowed-tools:")
            lines.extend(f"  - {entry}" for entry in allowed)
        lines += ["---", "", f"# {name}", ""]
        (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return str(root)


# The table the SDK's own rule parser documents. Ours has to agree with it,
# because the CLI applies the SDK's reading and not ours: a disagreement in either
# direction is a fail-open (a real shadow missed) or a false refusal (a working
# bundle blocked).
@pytest.mark.parametrize(
    ("entry", "whole", "named"),
    [
        ("Bash", "Bash", "Bash"),
        ("Bash()", "Bash", "Bash"),
        ("Bash(*)", "Bash", "Bash"),
        ("Bash(ls:*)", None, "Bash"),
        ("mcp__gh__create_issue", "mcp__gh__create_issue", "mcp__gh__create_issue"),
        ("(oops)", None, None),
        ("Bash(unterminated", None, None),
        ("", None, None),
        ("   ", None, None),
    ],
)
def test_entry_parsing_matches_the_cli_rule(
    entry: str, whole: str | None, named: str | None
) -> None:
    assert _whole_tool_allowed(entry) == whole
    assert _entry_tool(entry) == named


def test_our_parser_agrees_with_the_sdk_on_whole_tool_allowances() -> None:
    """Pin against the SDK rather than against our own reading of the docs.

    The SDK ships the same parser for its shadowing warning. Importing it is the
    only way to notice an upstream change in what counts as a whole-tool
    allowance; a hand-copied table would keep passing while the meaning moved.
    """
    sdk = pytest.importorskip("claude_agent_sdk.types")
    reference = getattr(sdk, "_whole_tool_allowed", None)
    if reference is None:
        pytest.skip("SDK no longer exposes the reference rule parser")
    for entry in ("Bash", "Bash()", "Bash(*)", "Bash( )", "Bash(ls:*)", "(x)", "Bash(", "", " "):
        assert _whole_tool_allowed(entry) == reference(entry), entry


def test_boot_refuses_when_a_skill_preauthorizes_a_manifest_gated_tool(tmp_path: Path) -> None:
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash"]}, gates=["Bash"])
    gate = build_approval_gate(operator_tools=None, policy_routes={"Bash": "ops"})
    assert gate is not None
    with pytest.raises(ApprovalPolicyError) as exc:
        assert_gates_not_shadowed(plugin_dir, gate)
    assert "skills/demo/SKILL.md" in str(exc.value)


def test_boot_refuses_an_operator_gate_armed_after_the_bundle_shipped(tmp_path: Path) -> None:
    """The case a deploy-time validator structurally cannot catch.

    The bundle declares no approvalPolicy, so a bundle validator sees nothing to
    check. The operator arms the gate later with
    ``curie cluster approvals --gate Bash``, against a bundle whose skill has
    always allowed Bash. Only boot holds both facts at once.
    """
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash"]}, gates=[])
    gate = build_approval_gate(operator_tools=["Bash"], policy_routes={})
    assert gate is not None
    with pytest.raises(ApprovalPolicyError) as exc:
        assert_gates_not_shadowed(plugin_dir, gate)
    assert "Bash" in str(exc.value)


def test_an_mcp_shorthand_in_a_skill_matches_the_armed_runtime_name(tmp_path: Path) -> None:
    """The conflict raw string equality cannot see, and it is a fail-open.

    ``build_approval_gate`` arms an MCP gate under its live name
    ``mcp__plugin_demo_crm__send``, while a skill author writes the natural
    ``mcp__crm__send``. Compared raw those never match, so the shadow would be
    missed entirely. The entry is normalized through the SAME function that
    normalizes an operator's shorthand, so one naming rule keeps one parser.
    """
    plugin_dir = _bundle(
        tmp_path,
        skills={"demo": ["mcp__crm__send"]},
        gates=[],
        mcp={"crm": {"command": "run-crm"}},
    )
    resolution = ApprovalPolicyResolution(
        route_by_tool={},
        grantable_by_route={},
        bundle_name="demo",
        mcp_servers={"crm"},
        connector_servers=set(),
    )
    gate = build_approval_gate(
        operator_tools=["mcp__crm__send"],
        policy_routes={},
        bundle_name="demo",
        mcp_servers={"crm"},
        connector_servers=set(),
    )
    assert gate is not None
    assert gate.required == frozenset({"mcp__plugin_demo_crm__send"})
    # Without the resolution there is nothing to normalize against, so the
    # conflict is invisible. That asymmetry is the whole point of this test.
    assert shadowed_gates(plugin_dir, gate.required) == ()
    with pytest.raises(ApprovalPolicyError) as exc:
        assert_gates_not_shadowed(plugin_dir, gate, resolution)
    assert "mcp__plugin_demo_crm__send" in str(exc.value)


def test_boot_accepts_a_bundle_whose_skills_allow_only_ungated_tools(tmp_path: Path) -> None:
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Read", "Write(docs/*)"]}, gates=["Bash"])
    gate = build_approval_gate(operator_tools=None, policy_routes={"Bash": "ops"})
    assert gate is not None
    assert_gates_not_shadowed(plugin_dir, gate)


def test_no_gate_and_no_bundle_are_both_no_ops(tmp_path: Path) -> None:
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash"]}, gates=["Bash"])
    assert_gates_not_shadowed(plugin_dir, None)
    assert_gates_not_shadowed(None, ApprovalGate(required=frozenset({"Bash"}), route_by_tool={}))


def test_a_narrowed_allowance_still_refuses_boot(tmp_path: Path) -> None:
    """Narrowing preauthorizes the calls it matches, which is the same defect.

    The SDK's advisory warning reports only whole-tool allowances, and suggests
    narrowing so calls "fall through to can_use_tool". That advice is for a
    callback used to decide; it is not sound for an approval GATE, whose claim is
    that every call to the tool needs a human. ``Bash(ls:*)`` leaves every
    matching call ungated, so accepting it would ship a partial gate that reports
    as whole. The message says narrowing is not the remedy, so an author does not
    discover this by being breached.
    """
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash(ls:*)"]}, gates=["Bash"])
    gate = build_approval_gate(operator_tools=None, policy_routes={"Bash": "ops"})
    assert gate is not None
    with pytest.raises(ApprovalPolicyError) as exc:
        assert_gates_not_shadowed(plugin_dir, gate)
    assert "Narrowing an entry does not help" in str(exc.value)


def test_every_conflicting_skill_is_named_not_only_the_first(tmp_path: Path) -> None:
    """An operator fixing one file and redeploying must not find a second wall.

    Reporting only the first conflict turns one edit into N deploys, and each
    redeploy of a security-relevant bundle is a chance to give up and remove the
    gate instead.
    """
    plugin_dir = _bundle(
        tmp_path,
        skills={"alpha": ["Bash"], "beta": ["Bash(*)"], "gamma": ["Read"]},
        gates=["Bash"],
    )
    conflicts = shadowed_gates(plugin_dir, frozenset({"Bash"}))
    assert [c.skill for c in conflicts] == ["skills/alpha/SKILL.md", "skills/beta/SKILL.md"]


def test_matching_is_exact_so_a_near_miss_is_not_a_hit(tmp_path: Path) -> None:
    """``BashTool`` is not ``Bash``, in either direction.

    ``can_use_tool`` compares by exact string equality, so anything looser would
    report conflicts the runtime does not have, and anything that stripped
    prefixes would miss the ones it does.
    """
    plugin_dir = _bundle(tmp_path, skills={"demo": ["BashTool"]}, gates=["Bash"])
    assert shadowed_gates(plugin_dir, frozenset({"Bash"})) == ()
    assert shadowed_gates(plugin_dir, frozenset({"BashTool"}))[0].tool == "BashTool"


def test_a_malformed_skill_is_left_to_the_frontmatter_validator(tmp_path: Path) -> None:
    """Reporting the wrong defect hides the real one.

    A skill with unparseable frontmatter already fails ``validate_bundle`` with a
    frontmatter error. Failing the gate check on it as well would tell an operator
    their approval policy is broken when their YAML is.
    """
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash"]}, gates=["Bash"])
    broken = Path(plugin_dir) / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    conflicts = shadowed_gates(plugin_dir, frozenset({"Bash"}))
    assert [c.skill for c in conflicts] == ["skills/demo/SKILL.md"]


def test_the_message_names_the_file_the_entry_and_the_remedy(tmp_path: Path) -> None:
    plugin_dir = _bundle(tmp_path, skills={"demo": ["Bash"]}, gates=["Bash"])
    message = describe_shadowed_gates(shadowed_gates(plugin_dir, frozenset({"Bash"})))
    assert "skills/demo/SKILL.md" in message
    assert "'Bash'" in message
    assert "Remove those entries" in message
    # The reason has to travel with the message: read from a pod log by someone
    # who did not write the bundle, "conflict" alone is not actionable.
    assert "before the approval callback runs" in message
