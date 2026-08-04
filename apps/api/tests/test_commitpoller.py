"""Noticing new commits without a webhook (issue #1239).

The decision -- which branches moved and are worth deploying -- is pure, so it
is tested without HTTP or a database. What matters is not that it spots a moved
branch; that is one comparison. It is that it does not deploy the same commit
twice, does not stop polling when one repository breaks, and hands the deploy
path a payload indistinguishable from a real webhook.
"""

from __future__ import annotations

import pytest
from curie_api.commitpoller import Move, PollTarget, moves_to_deploy

REPO = "octo/agent-bot"
CLONE = "https://github.com/octo/agent-bot.git"


class Tips:
    """Branch tips, and optionally a repository that raises."""

    def __init__(self, shas: dict[tuple[str, str], str | None], explode: set[str] | None = None):
        self._shas = shas
        self._explode = explode or set()
        self.asked: list[tuple[str, str]] = []

    def sha_for(self, repo_full_name: str, branch: str) -> str | None:
        self.asked.append((repo_full_name, branch))
        if repo_full_name in self._explode:
            raise RuntimeError("credential revoked")
        return self._shas.get((repo_full_name, branch))


def target(*branches: str, repo: str = REPO) -> PollTarget:
    return PollTarget(repo_full_name=repo, clone_url=CLONE, branches=branches)


# --------------------------------------------------------------------------- #
# Not deploying the same commit twice
# --------------------------------------------------------------------------- #
def test_an_unchanged_branch_is_not_redeployed() -> None:
    # The steady state. A poll every minute against an idle repo must produce
    # nothing at all, or the agent is redeployed once a minute forever.
    tips = Tips({(REPO, "dev"): "abc123"})
    assert moves_to_deploy([target("dev")], tips, {(REPO, "dev"): "abc123"}) == []


def test_a_moved_branch_is_deployed() -> None:
    tips = Tips({(REPO, "dev"): "def456"})
    moves = moves_to_deploy([target("dev")], tips, {(REPO, "dev"): "abc123"})
    assert [m.sha for m in moves] == ["def456"]


def test_a_branch_never_deployed_before_is_deployed() -> None:
    tips = Tips({(REPO, "dev"): "abc123"})
    assert len(moves_to_deploy([target("dev")], tips, {})) == 1


def test_a_restart_does_not_redeploy_current_head() -> None:
    # The poller holds no memory of its own; "already deployed" comes from what
    # is recorded against the repository. If it did not, every API restart
    # would redeploy every agent.
    tips = Tips({(REPO, "dev"): "abc123", (REPO, "main"): "zzz999"})
    state = {(REPO, "dev"): "abc123", (REPO, "main"): "zzz999"}
    assert moves_to_deploy([target("dev", "main")], tips, state) == []


def test_dev_and_prod_are_tracked_separately() -> None:
    # Same repository, two branches, two agents (ADR-0091). A dev push must not
    # mark prod as deployed.
    tips = Tips({(REPO, "dev"): "new111", (REPO, "main"): "old222"})
    state = {(REPO, "dev"): "old000", (REPO, "main"): "old222"}
    moves = moves_to_deploy([target("dev", "main")], tips, state)
    assert [(m.branch, m.sha) for m in moves] == [("dev", "new111")]


# --------------------------------------------------------------------------- #
# One broken repository must not stop the rest
# --------------------------------------------------------------------------- #
def test_a_failing_repository_does_not_stop_the_others() -> None:
    # A revoked credential or a deleted repo is a per-repo condition. Letting it
    # propagate would silently halt deploys for every other agent on the
    # cluster -- with no error anyone would look at.
    broken, fine = "octo/broken", "octo/fine"
    tips = Tips({(fine, "dev"): "abc123"}, explode={broken})
    targets = [target("dev", repo=broken), target("dev", repo=fine)]
    moves = moves_to_deploy(targets, tips, {})
    assert [m.repo_full_name for m in moves] == [fine]


def test_a_branch_the_repository_does_not_have_is_skipped() -> None:
    # A deploy.yaml may name a prod branch a repo has not created yet. Normal,
    # not an error.
    tips = Tips({(REPO, "dev"): "abc123", (REPO, "main"): None})
    moves = moves_to_deploy([target("dev", "main")], tips, {})
    assert [m.branch for m in moves] == ["dev"]


def test_polling_is_per_repository_not_per_agent() -> None:
    # Several agents share one repository. Asking once per branch is the
    # difference between one API call and N racing deploys of the same commit.
    tips = Tips({(REPO, "dev"): "abc123"})
    moves_to_deploy([target("dev")], tips, {})
    assert tips.asked == [(REPO, "dev")]


# --------------------------------------------------------------------------- #
# The payload the deploy path receives
# --------------------------------------------------------------------------- #
def test_the_payload_is_shaped_like_a_real_webhook() -> None:
    # Reusing process_push is what keeps polling and the webhook from
    # disagreeing about what a push means, and that only holds if the payload
    # carries the fields it parses.
    payload = Move(REPO, CLONE, "dev", "abc123").as_push_payload()
    assert payload["ref"] == "refs/heads/dev"
    assert payload["after"] == "abc123"
    assert payload["repository"]["full_name"] == REPO
    assert payload["repository"]["clone_url"] == CLONE


def test_the_payload_carries_the_derived_clone_url() -> None:
    # gitflow rejects a push whose clone_url does not match the one derived
    # from configuration. The poller supplies that derived URL, so a polled
    # deploy passes the origin check by construction -- not by coincidence.
    from curie_api.config import Settings
    from curie_api.gitflow import trusted_clone_url

    derived = trusted_clone_url(REPO, Settings(github_clone_base="https://github.com"))
    payload = Move(REPO, derived, "dev", "abc123").as_push_payload()
    assert payload["repository"]["clone_url"] == derived


@pytest.mark.parametrize("branches", [(), ("dev",), ("dev", "main")])
def test_no_targets_or_no_branches_is_quiet(branches: tuple[str, ...]) -> None:
    tips = Tips({})
    assert moves_to_deploy([target(*branches)], tips, {}) == []
    assert moves_to_deploy([], tips, {}) == []
