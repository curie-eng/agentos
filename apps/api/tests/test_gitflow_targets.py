"""Routing a push through the bundle's deploy.yaml (ADR-0091, #1070).

ADR-0091 named its own sharpest edge and said it "should be the first test
written, not the last": a `deploy.yaml` naming an agent bound to a DIFFERENT
repository would let one repository's push deploy over another's agent. It is
the first test here.

These cover `resolve_target_agent` directly -- it is a pure function of
(targets, environment, the repo's agents, the possibly-foreign named agent), so
every routing decision is testable with no webhook, no clone and no database.
"""

from __future__ import annotations

import uuid

import pytest
from curie_api.gitflow import TargetUnresolved, resolve_target_agent
from curie_api.models import Agent, Environment
from plugin_format.deploy_targets import validate_deploy_targets

REPO = "curie-eng/sre-bot"


def agent(name: str, repo: str | None = REPO) -> Agent:
    return Agent(id=uuid.uuid4(), name=name, slack_channel=f"C-{name}", repo_full_name=repo)


def targets(text: str):
    import yaml

    parsed, errors = validate_deploy_targets(yaml.safe_load(text))
    assert not errors, errors
    return parsed


DEV_AND_PROD = """
targets:
  dev:
    agent: sre-bot-dev
    env: dev
    slack_channel: C000000A01
  prod:
    agent: sre-bot
    env: prod
    slack_channel: C000000A02
"""


# --------------------------------------------------------------------------- #
# The sharpest edge, first
# --------------------------------------------------------------------------- #
def test_a_target_naming_another_repositorys_agent_is_refused() -> None:
    # Without this, anyone who can push to repo A deploys over repo B's bot by
    # naming it in their deploy.yaml.
    foreign = agent("sre-bot", repo="someone-else/their-bot")
    with pytest.raises(TargetUnresolved) as caught:
        resolve_target_agent(
            targets(DEV_AND_PROD), Environment.prod, [agent("sre-bot-dev")], foreign
        )
    assert caught.value.code == "deploy.agent_bound_elsewhere"
    assert "another repository" in str(caught.value)


def test_an_unknown_agent_is_refused_rather_than_created() -> None:
    # A webhook does not mint agents. Creating one would let a push conjure a
    # bot on a channel of its choosing.
    with pytest.raises(TargetUnresolved) as caught:
        resolve_target_agent(targets(DEV_AND_PROD), Environment.prod, [agent("sre-bot-dev")], None)
    assert caught.value.code == "deploy.unknown_agent"
    assert "does not exist" in str(caught.value)


# --------------------------------------------------------------------------- #
# The thing this is for: dev push -> dev agent, main push -> prod agent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("environment", "expected"),
    [(Environment.dev, "sre-bot-dev"), (Environment.prod, "sre-bot")],
)
def test_each_branch_reaches_its_own_agent(environment, expected) -> None:
    both = [agent("sre-bot"), agent("sre-bot-dev")]
    resolved = resolve_target_agent(targets(DEV_AND_PROD), environment, both, None)
    assert resolved is not None and resolved.name == expected


def test_the_two_targets_resolve_to_different_agents() -> None:
    # The whole point of #1070. Before this, both branches resolved to whichever
    # single agent the repository row named, so a dev merge and a main merge
    # overwrote each other's active version.
    both = [agent("sre-bot"), agent("sre-bot-dev")]
    dev = resolve_target_agent(targets(DEV_AND_PROD), Environment.dev, both, None)
    prod = resolve_target_agent(targets(DEV_AND_PROD), Environment.prod, both, None)
    assert dev is not None and prod is not None
    assert dev.id != prod.id


# --------------------------------------------------------------------------- #
# Ignore vs reject -- a distinction that decides whether silence is correct
# --------------------------------------------------------------------------- #
def test_a_branch_with_no_matching_target_is_ignored_not_rejected() -> None:
    # A repo may deploy only prod from main and leave dev to the CLI. Ignoring
    # matches how an unmatched branch already behaves.
    prod_only = targets(
        "targets:\n  prod:\n    agent: sre-bot\n    env: prod\n    slack_channel: C000000A02\n"
    )
    assert resolve_target_agent(prod_only, Environment.dev, [agent("sre-bot")], None) is None


def test_two_targets_for_one_environment_are_refused() -> None:
    # One push cannot deploy to two agents. Picking one silently would deploy
    # to an agent the author did not intend half the time.
    ambiguous = targets(
        "targets:\n"
        "  a:\n    agent: sre-bot\n    env: prod\n    slack_channel: C000000B01\n"
        "  b:\n    agent: other-bot\n    env: prod\n    slack_channel: C000000B02\n"
    )
    with pytest.raises(TargetUnresolved) as caught:
        resolve_target_agent(ambiguous, Environment.prod, [agent("sre-bot")], None)
    assert caught.value.code == "deploy.ambiguous_env"


# --------------------------------------------------------------------------- #
# Bundles that predate deploy.yaml
# --------------------------------------------------------------------------- #
def test_a_bundle_without_deploy_yaml_still_deploys_its_single_agent() -> None:
    # Back-compat. Every repo that worked before ADR-0089 must keep working.
    only = agent("sre-bot")
    assert resolve_target_agent(None, Environment.prod, [only], None) is only


def test_a_bundle_without_deploy_yaml_and_several_agents_is_refused() -> None:
    # There is no basis to choose, and guessing deploys to the wrong bot
    # silently -- which is the #1070 bug, not a fallback for it.
    with pytest.raises(TargetUnresolved) as caught:
        resolve_target_agent(None, Environment.prod, [agent("a"), agent("b")], None)
    assert caught.value.code == "deploy.no_targets"
    assert "deploy.yaml" in str(caught.value)


# --------------------------------------------------------------------------- #
# Binding an existing agent to a repository
# --------------------------------------------------------------------------- #
def test_repo_full_name_is_patchable() -> None:
    """Without this, ADR-0091 is unreachable for the repos that need it most.

    A repository's SECOND agent could not carry `repo_full_name` before
    migration 0018 -- the unique index forbade it -- so it exists today bound to
    nothing. `AgentUpdate` had no field for it, and create-time was the only
    place it could ever be set. Git-flow would therefore never find that agent,
    and a `deploy.yaml` naming it would be rejected as unknown.
    """

    from curie_api.schemas import AgentUpdate

    assert "repo_full_name" in AgentUpdate.model_fields
    assert AgentUpdate().repo_full_name is None, "omitted must leave the binding unchanged"
    assert AgentUpdate(repo_full_name="octo/x").repo_full_name == "octo/x"
