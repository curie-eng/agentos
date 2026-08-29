"""Declared deploy targets (ADR-0089, #1166).

Every case here is one that would otherwise deploy SUCCESSFULLY to the wrong
place. That is the whole reason the file is validated rather than trusted: a
mistyped agent mints a new agent, a mistyped channel binds a conversation nobody
watches, and both report success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plugin_format import validate_bundle
from plugin_format.deploy_targets import validate_deploy_targets


def _codes(data: object) -> list[str]:
    return [c for c, _ in validate_deploy_targets(data)[1]]


def _bundle(root: Path, deploy_yaml: str | None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "b", "version": "0.1.0", "description": "t"}), encoding="utf-8"
    )
    (root / "skills" / "b").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "b" / "SKILL.md").write_text(
        "---\nname: b\ndescription: t\n---\nhi\n", encoding="utf-8"
    )
    if deploy_yaml is not None:
        (root / "deploy.yaml").write_text(deploy_yaml, encoding="utf-8")
    return root


REAL = (
    "targets:\n"
    "  dev:\n"
    "    agent: acme-dev\n"
    "    env: dev\n"
    "    slack_channel: C0EXAMPLE2\n"
    "  prod:\n"
    "    agent: acme-bot\n"
    "    env: prod\n"
    "    slack_channel: C0EXAMPLE1\n"
)


def test_the_shape_the_adr_specifies_is_accepted() -> None:
    parsed, errors = validate_deploy_targets(
        {
            "targets": {
                "dev": {"agent": "acme-dev", "env": "dev", "slack_channel": "C0EXAMPLE2"},
                "prod": {"agent": "acme-bot", "env": "prod", "slack_channel": "C0EXAMPLE1"},
            }
        }
    )
    assert errors == []
    assert parsed is not None
    assert parsed.targets["prod"].env == "prod"
    assert parsed.targets["dev"].agent == "acme-dev"


def test_absent_file_is_fine(tmp_path: Path) -> None:
    # Bundles that pass routing as flags keep working; the file is additive.
    assert validate_bundle(str(_bundle(tmp_path, None))).valid


def test_an_unknown_env_is_rejected() -> None:
    # The worker ranks prod over dev explicitly, so a third value would never be
    # selected -- the deploy would succeed and the agent would never serve.
    assert "deploy.bad_env" in _codes({"targets": {"stg": {"env": "staging"}}})


def test_a_channel_name_instead_of_an_id_is_rejected() -> None:
    # `#sre` is what a human says; the platform needs `C...`. Binding a bad id
    # succeeds and the bot answers into nothing.
    assert "deploy.bad_slack_channel" in _codes({"targets": {"p": {"slack_channel": "#sre"}}})


def test_a_malformed_agent_name_is_rejected() -> None:
    # The failure this prevents: a typo does not error, it MINTS A NEW AGENT and
    # the deploy reports success against it.
    assert "deploy.bad_agent_name" in _codes({"targets": {"p": {"agent": "Acme_Bot"}}})


def test_env_defaults_to_dev_so_a_target_must_opt_in_to_prod() -> None:
    parsed, errors = validate_deploy_targets({"targets": {"p": {"agent": "a"}}})
    assert errors == []
    assert parsed is not None
    assert parsed.targets["p"].env == "dev"


@pytest.mark.parametrize(
    "target",
    [{"env": "prod"}, {"agent": None, "env": "prod"}],
    ids=["omitted", "explicit_null"],
)
def test_missing_agent_names_the_target(target: dict[str, object]) -> None:
    _, errors = validate_deploy_targets({"targets": {"p": target}})
    messages = [message for code, message in errors if code == "deploy.missing_agent"]
    assert len(messages) == 1
    assert "targets.p" in messages[0]
    assert "None" not in messages[0]


def test_unknown_key_is_rejected_not_ignored() -> None:
    # Same reasoning as connectors.yaml: this is Curie's own file with no
    # external producer, so an unrecognised key is a typo. `chanel` silently
    # ignored means an agent bound to the wrong conversation.
    assert _codes({"targets": {"p": {"chanel": "C0123456"}}}) != []


def test_non_mapping_file_is_rejected() -> None:
    assert "deploy.not_object" in _codes(["not", "a", "mapping"])


def test_bundle_surfaces_a_target_error(tmp_path: Path) -> None:
    root = _bundle(tmp_path, "targets:\n  p:\n    env: staging\n")
    result = validate_bundle(str(root))
    assert not result.valid
    assert any(e.code == "deploy.bad_env" for e in result.errors)


@pytest.mark.parametrize(
    "target_yaml",
    ["    env: prod\n", "    agent: null\n    env: prod\n"],
    ids=["omitted", "explicit_null"],
)
def test_bundle_refuses_a_missing_agent_at_load_time(
    tmp_path: Path, target_yaml: str
) -> None:
    root = _bundle(tmp_path, f"targets:\n  p:\n{target_yaml}")
    result = validate_bundle(str(root))
    issues = [error for error in result.errors if error.code == "deploy.missing_agent"]
    assert not result.valid
    assert len(issues) == 1
    assert "targets.p" in issues[0].message
    assert "None" not in issues[0].message


def test_bundle_surfaces_unparseable_yaml(tmp_path: Path) -> None:
    root = _bundle(tmp_path, "targets:\n  p:\n   env: [unclosed\n")
    result = validate_bundle(str(root))
    assert not result.valid
    assert any(e.code == "deploy.unreadable" for e in result.errors)


def test_bundle_rejects_a_duplicate_target_name(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "targets:\n"
        "  prod:\n"
        "    agent: first\n"
        "    env: prod\n"
        "  prod:\n"
        "    agent: second\n"
        "    env: prod\n",
    )
    result = validate_bundle(str(root))
    assert not result.valid
    issue = next(e for e in result.errors if e.code == "deploy.duplicate_target")
    assert "prod" in issue.message


def test_bundle_allows_an_explicit_override_of_a_merged_target_field(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "targets:\n"
        "  dev: &base\n"
        "    agent: acme\n"
        "    env: dev\n"
        "    slack_channel: C0EXAMPLE1\n"
        "  prod:\n"
        "    <<: *base\n"
        "    env: prod\n",
    )
    assert validate_bundle(str(root)).valid


def test_bundle_allows_chained_merge_anchors(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        "targets:\n"
        "  dev: &dev\n"
        "    agent: acmedev\n"
        "    env: dev\n"
        "    slack_channel: C0EXAMPLE1\n"
        "  candidate: &candidate\n"
        "    <<: *dev\n"
        "    agent: acmecandidate\n"
        "  prod:\n"
        "    <<: *candidate\n"
        "    agent: acmeprod\n"
        "    env: prod\n"
        "    slack_channel: C0EXAMPLE2\n",
    )
    assert validate_bundle(str(root)).valid


def test_the_real_acme_bot_routing_validates(tmp_path: Path) -> None:
    assert validate_bundle(str(_bundle(tmp_path, REAL))).valid


# --------------------------------------------------------------------------- #
# Agent names that forge the renderer's `-mcp-` join -- #1446
#
# Every connector Curie renders for an agent is named
# `{release}-{agent}-mcp-{connector}`. `-mcp-` is a bare substring inside one
# DNS label, not a structural separator, so an agent name that ends in `-mcp` or
# contains `-mcp-` makes that name ambiguous about where the agent ends: a
# DIFFERENT agent/connector pair renders the same Service, the same Deployment,
# both the same NetworkPolicies, and -- worst of all -- the same
# `app.kubernetes.io/name`, which IS the pod selector. One agent's sandbox then
# reaches the other agent's connector and the credential bound to it (ADR-0086
# leaves the connector deliberately unauthenticated, so the network is the whole
# of the access control).
#
# This is the same class of silent failure the shape check already guards: a
# typo does not fail, it MINTS A NEW AGENT. A forged join is one level deeper --
# it does not mint a new agent, it MERGES two.
# --------------------------------------------------------------------------- #
FORGING_AGENT_NAMES = ["a-mcp-b", "x-mcp", "grafana-mcp"]
SAFE_AGENT_NAMES = ["mcp-x", "mcp", "acme-dev", "acme-bot"]


@pytest.mark.parametrize("agent", FORGING_AGENT_NAMES)
def test_an_agent_name_that_forges_the_render_join_is_rejected(agent: str) -> None:
    # Each of these is a well-formed RFC 1123 label, so the existing shape check
    # passes it and always has. Only the new code can catch it, which is what
    # the exact-list assertion pins -- a passing test here cannot be a
    # `deploy.bad_agent_name` standing in.
    assert _codes({"targets": {"p": {"agent": agent}}}) == ["deploy.ambiguous_agent_name"]


@pytest.mark.parametrize("agent", SAFE_AGENT_NAMES)
def test_an_agent_name_that_merely_starts_with_mcp_is_accepted(agent: str) -> None:
    # The asymmetry that reads like a bug: a LEADING `mcp-` is fine on the agent
    # and fatal on the connector, because the alternative split of
    # `curie-mcp-x-mcp-c` would leave an EMPTY agent, which no bundle can
    # declare. Each side abuts a different half of the delimiter. Over-rejecting
    # here is not the safe direction -- every name refused is a working install
    # that must be renamed before its next deploy.
    assert "deploy.ambiguous_agent_name" not in _codes({"targets": {"p": {"agent": agent}}})


def test_a_malformed_and_forging_agent_name_reports_both() -> None:
    # `Acme-mcp-bot` is both badly shaped (uppercase is not an RFC 1123 label)
    # and forging. The file's convention is to accumulate every applicable code
    # so the author fixes the whole file in one pass; this fails if the new
    # check lands as an `elif` and swallows one of the two.
    codes = _codes({"targets": {"p": {"agent": "Acme-mcp-bot"}}})
    assert "deploy.bad_agent_name" in codes
    assert "deploy.ambiguous_agent_name" in codes


def test_a_malformed_name_that_does_not_forge_reports_only_the_shape_error() -> None:
    # Regression pin on the existing behaviour: adding the new code must not
    # start attaching it to every malformed name. `Acme_Bot` is a shape problem
    # and nothing else.
    codes = _codes({"targets": {"p": {"agent": "Acme_Bot"}}})
    assert "deploy.bad_agent_name" in codes
    assert "deploy.ambiguous_agent_name" not in codes


def test_a_missing_agent_still_reports_only_missing_agent() -> None:
    # The new check has to be guarded on the agent being present. The obvious
    # placement -- a sibling `if` beside the existing `elif not _is_valid_name`
    # chain -- calls the predicate on `None` and raises TypeError out of a
    # validator whose entire contract is to RETURN errors, turning a friendly
    # "targets.p: agent is required" into a crash during bundle validation.
    codes = _codes({"targets": {"p": {"agent": None, "env": "prod"}}})
    assert codes == ["deploy.missing_agent"]


def test_the_ambiguous_agent_name_error_says_what_to_do() -> None:
    # The operator hitting this is hitting it on an install that deployed
    # yesterday -- `grafana-mcp` was a legal agent name before this fix. The
    # message has to name the target key (which of several targets to edit), the
    # agent, and say to rename, or the only way to resolve it is to read the
    # renderer's source.
    _, errors = validate_deploy_targets({"targets": {"p": {"agent": "grafana-mcp"}}})
    message = next(m for c, m in errors if c == "deploy.ambiguous_agent_name")
    assert "targets.p" in message
    assert "grafana-mcp" in message
    assert "-mcp-" in message
    assert "rename" in message.lower()
