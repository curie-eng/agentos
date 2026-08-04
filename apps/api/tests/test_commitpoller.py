"""Noticing new commits without a webhook (issue #1239).

The decision -- which branches moved and are worth deploying -- is pure, so it
is tested without HTTP or a database. What matters is not that it spots a moved
branch; that is one comparison. It is that it does not deploy the same commit
twice, does not stop polling when one repository breaks, and hands the deploy
path a payload indistinguishable from a real webhook.
One test, the one that runs the real lifespan (#1250), is the exception and is
deliberate: the wiring the poller receives from the lifespan cannot be checked
without running the real lifespan.
"""

from __future__ import annotations

import logging
import os

import httpx
import pytest
from curie_api.commitpoller import Move, PollTarget, moves_to_deploy
from curie_api.config import get_settings
from curie_api.main import create_app
from fastapi.testclient import TestClient

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


# --------------------------------------------------------------------------- #
# The wiring the lifespan hands it (#1250)
# --------------------------------------------------------------------------- #
def test_the_lifespan_wires_the_poller_to_the_bundle_store(clean_db: None) -> None:
    # The poller was constructed with `store=app.state.store`, an attribute
    # nothing assigns, so enabling it crashed the API at startup and the feature
    # never ran once. mypy cannot see it (State.__getattr__ returns Any) and no
    # test built the poller, so the only thing that catches it is running the
    # real lifespan with the interval on. The identity assertion is the point:
    # `is not None` would pass against a poller wired to the wrong object.
    #
    # clean_db (not _disposable_db) is load-bearing, not a style choice: on
    # entering the TestClient's `with` block, run_forever calls poll_once
    # immediately, before its first sleep, and poll_once queries curie.agents
    # for rows with a repo_full_name. If an agent row from an earlier test
    # survived, this pass would call GitHubBranchTip.sha_for and issue a real
    # httpx request to the GitHub API. clean_db's TRUNCATE is the only reason
    # that pass is a no-op; do not weaken this to _disposable_db.
    prior = os.environ.get("COMMIT_POLL_INTERVAL_S")
    os.environ["COMMIT_POLL_INTERVAL_S"] = "3600"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.app.state.commit_poller_task is not None
            poller = client.app.state.commit_poller
            assert poller._store is client.app.state.bundle_store
            # Pins the actual defect: a "fix" that leaves store=app.state.store
            # in place and adds app.state.store = bundle_store as an alias would
            # satisfy the identity assertion above while reintroducing the
            # compatibility path this codebase forbids. Exactly one store name.
            assert not hasattr(client.app.state, "store")
    finally:
        if prior is None:
            os.environ.pop("COMMIT_POLL_INTERVAL_S", None)
        else:
            os.environ["COMMIT_POLL_INTERVAL_S"] = prior
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Rejections are reported, not called "deployed" (#1268)
# --------------------------------------------------------------------------- #
async def _run_poll_once(monkeypatch, result) -> None:
    """Drive the real poll_once with a stubbed deploy that returns `result`."""
    from contextlib import asynccontextmanager

    from curie_api import gitflow
    from curie_api.commitpoller import CommitPoller
    from curie_api.config import Settings

    class Rows(list):
        def __iter__(self):  # noqa: D105 - the two queries both iterate
            return super().__iter__()

    class Session:
        async def execute(self, stmt):
            # First query lists repos, second lists deployed shas. Returning
            # one repo and nothing deployed makes exactly one move.
            return Rows([(REPO,)]) if "FROM curie.agents" in str(stmt) else Rows([])

    @asynccontextmanager
    async def factory():
        yield Session()

    async def fake_process_push(session, store, settings, eval_queue, payload):
        return result

    monkeypatch.setattr(gitflow, "process_push", fake_process_push)
    poller = CommitPoller(
        session_factory=factory,
        store=object(),
        settings=Settings(github_clone_base="https://github.com"),
        eval_queue=object(),
        tips=Tips({(REPO, "dev"): "abc123", (REPO, "main"): None}),
        interval_seconds=60,
    )
    await poller.poll_once()


@pytest.mark.anyio
async def test_a_rejected_polled_deploy_warns_from_the_poller(monkeypatch, caplog) -> None:
    # AC3: driven through poll_once, not by calling the logger directly. An
    # earlier version of this test called log_push_outcome itself and passed
    # even with the poller's call deleted -- it proved the logger worked, not
    # that the poller used it.
    from curie_api.schemas import WebhookResult

    rejected = WebhookResult(
        status="rejected", errors=[{"code": "deploy.unknown_agent", "message": "no such agent"}]
    )
    with caplog.at_level(logging.WARNING):
        await _run_poll_once(monkeypatch, rejected)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a rejected polled deploy must warn"
    text = " ".join(r.getMessage() for r in warnings)
    assert "deploy.unknown_agent" in text, f"codes must be in the record: {text}"
    assert "commit poll" in text, f"the lane must be identifiable: {text}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_a_rejected_polled_deploy_warns_with_its_codes(caplog) -> None:
    # The poller reported every outcome as "deployed" at INFO and discarded
    # result.errors. That is #1066 again -- the system reporting success for
    # work it did not do -- on the lane with no GitHub delivery UI to fall back
    # on, because polling exists for clusters GitHub cannot reach.
    from curie_api.gitflow import log_push_outcome
    from curie_api.schemas import WebhookResult

    rejected = WebhookResult(
        status="rejected", errors=[{"code": "deploy.unknown_agent", "message": "no such agent"}]
    )
    with caplog.at_level(logging.WARNING, logger="curie_api.gitflow"):
        log_push_outcome(
            rejected, Move(REPO, CLONE, "dev", "abc123").as_push_payload(), source="commit poll"
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a rejected polled deploy must warn"
    text = warnings[0].getMessage()
    assert "deploy.unknown_agent" in text, f"the codes must be in the record: {text}"
    assert "commit poll" in text, f"the lane must be identifiable: {text}"


def test_a_successful_deploy_does_not_warn(caplog) -> None:
    from curie_api.gitflow import log_push_outcome
    from curie_api.schemas import WebhookResult

    with caplog.at_level(logging.WARNING, logger="curie_api.gitflow"):
        log_push_outcome(
            WebhookResult(status="deployed"),
            Move(REPO, CLONE, "dev", "abc").as_push_payload(),
            source="commit poll",
        )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_both_lanes_use_the_same_reporter() -> None:
    # AC2: shared, not duplicated, so the two cannot drift. The webhook router
    # must call the same function rather than keep its own copy.
    from pathlib import Path

    router = Path("apps/api/src/curie_api/routers/github.py").read_text()
    assert "log_push_outcome" in router
    assert "def _log_outcome" not in router, "the router kept a private copy"


# --------------------------------------------------------------------------- #
# Throttling backs off instead of re-asking (#1269)
# --------------------------------------------------------------------------- #
def _tip_reader(handler):
    """A GitHubBranchTip whose HTTP is answered by `handler`."""
    from curie_api.commitpoller import GitHubBranchTip
    from curie_api.config import Settings

    real = httpx.Client

    class Creds:
        def token_for(self, repo: str) -> str:
            return ""

    tip = GitHubBranchTip(Settings(), Creds())
    tip._client_factory = lambda **kw: real(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    return tip


def test_a_429_with_retry_after_is_respected_on_the_next_attempt(monkeypatch) -> None:
    # GitHub documents Retry-After (seconds) on a secondary rate limit, under
    # "Rate limits for the REST API". The failure this prevents: re-requesting
    # at the next interval, which is what turns a brief throttle into a
    # sustained one. An unauthenticated caller gets 60 requests/hour.
    from curie_api.commitpoller import GitHubBranchTip, RateLimited
    from curie_api.config import Settings

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    real = httpx.Client
    monkeypatch.setattr(
        "curie_api.commitpoller.httpx.Client",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)),
    )

    class Creds:
        def token_for(self, repo: str) -> str:
            return ""

    tip = GitHubBranchTip(Settings(), Creds())
    with pytest.raises(RateLimited) as first:
        tip.sha_for(REPO, "dev")
    assert first.value.retry_after_s == pytest.approx(120, abs=2)

    # The second attempt must not reach GitHub at all.
    with pytest.raises(RateLimited):
        tip.sha_for(REPO, "dev")
    assert calls["n"] == 1, "a throttled repo was re-requested instead of backing off"


def test_sustained_throttling_is_reported_above_a_per_branch_warning(monkeypatch, caplog) -> None:
    # AC2 of #1269: one 429 is routine, several rounds running means the deploy
    # lane has stopped and must be findable as that rather than as a blip.
    from curie_api.commitpoller import GitHubBranchTip, RateLimited
    from curie_api.config import Settings

    real = httpx.Client
    monkeypatch.setattr(
        "curie_api.commitpoller.httpx.Client",
        lambda *a, **kw: real(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(429, headers={"Retry-After": "0"}, json={})
            )
        ),
    )

    class Creds:
        def token_for(self, repo: str) -> str:
            return ""

    tip = GitHubBranchTip(Settings(), Creds())
    with caplog.at_level(logging.WARNING, logger="curie_api.commitpoller"):
        for _ in range(3):
            with pytest.raises(RateLimited):
                tip.sha_for(REPO, "dev")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "sustained throttling must escalate above a per-branch warning"
    assert "NOT happening" in errors[0].getMessage()


def test_a_throttled_repository_does_not_stop_the_others(monkeypatch) -> None:
    # RateLimited is an exception, and moves_to_deploy catches per repo/branch.
    # If that ever changed, one throttled repo would halt every other agent.
    from curie_api.commitpoller import RateLimited

    class Throttled:
        def sha_for(self, repo: str, branch: str) -> str | None:
            if repo == "octo/slow":
                raise RateLimited(repo, 120)
            return "abc123"

    moves = moves_to_deploy(
        [target("dev", repo="octo/slow"), target("dev", repo="octo/fine")], Throttled(), {}
    )
    assert [m.repo_full_name for m in moves] == ["octo/fine"]
