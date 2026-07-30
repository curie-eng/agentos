import pytest
from plugin_format import ApprovalGate, McpServer, PluginManifest, SkillFrontmatter
from pydantic import ValidationError


def test_manifest_requires_name() -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"description": "no name"})


def test_manifest_accepts_and_keeps_unknown_keys() -> None:
    manifest = PluginManifest.model_validate({"name": "demo", "futureField": 42})
    assert manifest.name == "demo"
    assert manifest.model_dump().get("futureField") == 42


def test_manifest_author_may_be_string_or_object() -> None:
    assert PluginManifest.model_validate({"name": "d", "author": "Jane"}).author == "Jane"
    obj = PluginManifest.model_validate({"name": "d", "author": {"name": "Jane"}})
    assert obj.author.name == "Jane"  # type: ignore[union-attr]


def test_skill_frontmatter_alias_and_required_fields() -> None:
    fm = SkillFrontmatter.model_validate(
        {"name": "greeter", "description": "greets", "allowed-tools": ["Bash"]}
    )
    assert fm.allowed_tools == ["Bash"]

    with pytest.raises(ValidationError):
        SkillFrontmatter.model_validate({"name": "greeter"})


def test_mcp_server_accepts_stdio_and_remote_shapes() -> None:
    stdio = McpServer.model_validate({"command": "python", "args": ["-m", "x"]})
    remote = McpServer.model_validate({"type": "http", "url": "https://example.com"})
    assert stdio.command == "python"
    assert remote.url == "https://example.com"


def test_manifest_system_prompt_field() -> None:
    """The Curie ``systemPrompt`` authoring extension round-trips (#271)."""
    manifest = PluginManifest.model_validate(
        {"name": "demo", "systemPrompt": "Be terse; cite the CRM record, not the message."}
    )
    assert manifest.systemPrompt == "Be terse; cite the CRM record, not the message."
    # Absent -> None (backward compatible; bundles without it still validate).
    assert PluginManifest.model_validate({"name": "demo"}).systemPrompt is None
    # Serializes back under the verbatim camelCase key.
    assert manifest.model_dump(exclude_none=True)["systemPrompt"].startswith("Be terse")


def test_manifest_starter_prompts_round_trip() -> None:
    manifest = PluginManifest.model_validate(
        {"name": "demo", "starterPrompts": ["Show open issues", "Summarize activity"]}
    )
    assert manifest.starterPrompts == ["Show open issues", "Summarize activity"]
    assert PluginManifest.model_validate({"name": "demo"}).starterPrompts is None


def test_manifest_secrets_field() -> None:
    """The Curie ``secrets`` policy extension round-trips (ADR-0009 / #429)."""
    manifest = PluginManifest.model_validate(
        {"name": "demo", "secrets": ["GITHUB_PERSONAL_ACCESS_TOKEN"]}
    )
    assert manifest.secrets == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    # Absent -> None (backward compatible; bundles without it still validate).
    assert PluginManifest.model_validate({"name": "demo"}).secrets is None
    # Serializes back under the verbatim key.
    assert manifest.model_dump(exclude_none=True)["secrets"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


def test_manifest_trigger_and_approval_policy_fields() -> None:
    """The Curie trigger + approval-policy authoring extensions parse (#273)."""
    manifest = PluginManifest.model_validate(
        {
            "name": "demo",
            "triggers": [{"type": "cron", "schedule": "0 9 * * 1-5"}],
            "approvalPolicy": {"gates": [{"gate": "PreToolUse", "route": "manager"}]},
        }
    )
    assert manifest.triggers == [{"type": "cron", "schedule": "0 9 * * 1-5"}]
    assert manifest.approvalPolicy == {"gates": [{"gate": "PreToolUse", "route": "manager"}]}
    # Absent -> None (backward compatible).
    bare = PluginManifest.model_validate({"name": "demo"})
    assert bare.triggers is None and bare.approvalPolicy is None


def test_approval_gate_grantable_via_policy_field() -> None:
    """The operator opt-in ``grantableViaPolicy`` round-trips on ApprovalGate (#558).

    A gate the operator explicitly marks may mint a one-shot grant on a policy
    approval; absent, it defaults False (the #544 no-grant baseline), so an old
    manifest keeps its behavior.
    """

    gate = ApprovalGate.model_validate(
        {"gate": "close_issue", "route": "deal-desk", "grantableViaPolicy": True}
    )
    assert gate.grantableViaPolicy is True
    # Absent -> False (backward compatible; existing gates keep the no-grant default).
    gate2 = ApprovalGate.model_validate({"gate": "close_issue", "route": "deal-desk"})
    assert gate2.grantableViaPolicy is False
    # Serializes back under the verbatim camelCase key and round-trips.
    dumped = gate.model_dump()
    assert dumped["grantableViaPolicy"] is True
    assert ApprovalGate.model_validate(dumped).grantableViaPolicy is True
