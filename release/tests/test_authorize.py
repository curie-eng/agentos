"""Contract tests for the release authorization gate (release/authorize.py).

The gate is the deterministic check behind issue #628: a `v*` tag must not be
able to start the publish pipeline unless its commit is reachable from
`origin/main` and that commit's required checks are all green. These tests
drive both functions directly -- `commit_is_on_reviewed_main` against a real,
disposable git repo (no network needed for ancestry) and
`missing_required_checks` against constructed
check-run lists -- plus `authorize()`, which combines them and is what
`authorize-release` actually calls.
"""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "release" / "authorize.py"


def load_module():
    """Import the standalone script by path (release/ is not on sys.path)."""
    spec = importlib.util.spec_from_file_location("release_authorize", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


authorize_module = load_module()


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-m", f"add {name}")
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A repo with a `main` branch, plus commits diverging on an unmerged branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    commit(repo, "on-main.txt")
    return repo


# A required-name set distinct from the real, larger production
# REQUIRED_CHECK_NAMES (issue #733). Most tests in this file exercise the
# *logic* of required-check matching and should not need updating every time
# a ci.yaml job is renamed or added; the production constant itself gets its
# own coverage in TestRequiredCheckAllowlist below.
TEST_REQUIRED_NAMES = frozenset({"CI", "CodeQL"})

CHECK_RUNS_ALL_GREEN = [
    {"name": "CI", "conclusion": "success"},
    {"name": "CodeQL", "conclusion": "neutral"},
    {"name": "Secret Scan", "conclusion": "skipped"},
]
CHECK_RUNS_ONE_FAILED = [
    {"name": "CI", "conclusion": "success"},
    {"name": "CodeQL", "conclusion": "failure"},
]

CURRENT_RUN_ID = "29811627398"
OTHER_RUN_ID = "29811600001"


def check_run(name: str, conclusion: str | None, run_id: str, job_id: str) -> dict:
    """A check-run shaped like the live API response (issue #732).

    `details_url` observed live on this repo:
    https://github.com/curie-eng/curie/actions/runs/29811627398/job/88573652086
    """
    return {
        "name": name,
        "status": "completed" if conclusion is not None else "in_progress",
        "conclusion": conclusion,
        "details_url": (
            f"https://github.com/curie-eng/curie/actions/runs/{run_id}/job/{job_id}"
        ),
    }


GATE_OWN_IN_PROGRESS = check_run(
    "authorize-release", None, CURRENT_RUN_ID, "88573652086"
)


class TestCommitIsOnReviewedMain:
    def test_head_of_main_is_reachable(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        assert authorize_module.commit_is_on_reviewed_main(sha, "main", cwd=git_repo)

    def test_ancestor_of_main_is_reachable(self, git_repo):
        first = run_git(git_repo, "rev-parse", "HEAD")
        commit(git_repo, "later-on-main.txt")

        assert authorize_module.commit_is_on_reviewed_main(first, "main", cwd=git_repo)

    def test_commit_only_on_an_unmerged_branch_is_refused(self, git_repo):
        run_git(git_repo, "checkout", "-q", "-b", "feature")
        unmerged = commit(git_repo, "feature-only.txt")

        assert not authorize_module.commit_is_on_reviewed_main(
            unmerged, "main", cwd=git_repo
        )

    def test_unknown_sha_is_refused_not_raised(self, git_repo):
        assert not authorize_module.commit_is_on_reviewed_main(
            "0" * 40, "main", cwd=git_repo
        )


class TestRequiredCheckSatisfaction:
    def test_all_required_names_present_and_green_passes(self):
        assert (
            authorize_module.missing_required_checks(
                CHECK_RUNS_ALL_GREEN, TEST_REQUIRED_NAMES
            )
            == set()
        )

    def test_a_required_name_that_concluded_failure_fails(self):
        assert authorize_module.missing_required_checks(
            CHECK_RUNS_ONE_FAILED, TEST_REQUIRED_NAMES
        ) == {"CodeQL"}

    def test_no_check_runs_fails(self):
        # Absence of checks is not evidence they passed.
        assert (
            authorize_module.missing_required_checks([], TEST_REQUIRED_NAMES)
            == TEST_REQUIRED_NAMES
        )

    def test_a_missing_required_name_fails_even_if_everything_present_is_green(self):
        # issue #733's core scenario: a non-empty, fully-passing list that
        # simply never contains the name that matters.
        only_unrelated = [{"name": "Secret Scan", "conclusion": "success"}]

        assert (
            authorize_module.missing_required_checks(
                only_unrelated, TEST_REQUIRED_NAMES
            )
            == TEST_REQUIRED_NAMES
        )

    def test_an_unrelated_failing_check_does_not_affect_the_required_set(self):
        # Only required names are asserted; a failing check outside
        # `required_names` has no bearing (this is not "everything present
        # must pass" -- that was the old, weaker behavior issue #733 replaces).
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CodeQL", "conclusion": "neutral"},
            {"name": "Some Unrelated Job", "conclusion": "failure"},
        ]

        assert (
            authorize_module.missing_required_checks(runs, TEST_REQUIRED_NAMES)
            == set()
        )


class TestMissingRequiredChecks:
    def test_empty_check_runs_reports_every_required_name_missing(self):
        assert (
            authorize_module.missing_required_checks([], TEST_REQUIRED_NAMES)
            == TEST_REQUIRED_NAMES
        )

    def test_all_present_and_green_reports_nothing_missing(self):
        assert (
            authorize_module.missing_required_checks(
                CHECK_RUNS_ALL_GREEN, TEST_REQUIRED_NAMES
            )
            == set()
        )

    def test_a_required_name_present_but_not_concluded_is_reported_missing(self):
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CodeQL", "conclusion": None},  # still in_progress
        ]

        assert authorize_module.missing_required_checks(
            runs, TEST_REQUIRED_NAMES
        ) == {"CodeQL"}


class TestAuthorize:
    def test_reviewed_and_green_commit_is_authorized(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        authorize_module.authorize(
            sha, CHECK_RUNS_ALL_GREEN, "main", cwd=git_repo, required_names=TEST_REQUIRED_NAMES
        )

    def test_unreviewed_commit_is_refused_even_with_green_checks(self, git_repo):
        run_git(git_repo, "checkout", "-q", "-b", "feature")
        unmerged = commit(git_repo, "feature-only.txt")

        with pytest.raises(authorize_module.AuthorizationError, match="not reachable"):
            authorize_module.authorize(
                unmerged,
                CHECK_RUNS_ALL_GREEN,
                "main",
                cwd=git_repo,
                required_names=TEST_REQUIRED_NAMES,
            )

    def test_reviewed_commit_with_a_failed_required_check_is_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        with pytest.raises(authorize_module.AuthorizationError, match="required check-run"):
            authorize_module.authorize(
                sha,
                CHECK_RUNS_ONE_FAILED,
                "main",
                cwd=git_repo,
                required_names=TEST_REQUIRED_NAMES,
            )


class TestRequiredCheckAllowlist:
    """Issue #733: a non-empty, fully-green check-run list is not enough on
    its own -- the checks that matter must actually be among them. These use
    the real production `REQUIRED_CHECK_NAMES` (no override), covering the
    exact failure scenario from the issue: main's real CI never started for a
    commit, but one unrelated check-run (e.g. a security scanner) passed on
    that SHA.
    """

    UNRELATED_BUT_GREEN = [
        {"name": "gitleaks (full history)", "conclusion": "success"},
        {"name": "Analyze (python)", "conclusion": "success"},
    ]

    def test_unrelated_green_checks_alone_leave_required_names_missing(self):
        assert authorize_module.missing_required_checks(self.UNRELATED_BUT_GREEN)

    def test_missing_required_checks_lists_every_ci_yaml_job(self):
        missing = authorize_module.missing_required_checks(self.UNRELATED_BUT_GREEN)

        assert missing == authorize_module.REQUIRED_CHECK_NAMES

    def test_authorize_refuses_a_commit_whose_only_checks_are_unrelated_but_green(
        self, git_repo
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        with pytest.raises(
            authorize_module.AuthorizationError, match="required check-run"
        ):
            authorize_module.authorize(sha, self.UNRELATED_BUT_GREEN, "main", cwd=git_repo)

    def test_every_required_check_present_and_green_authorizes(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            {"name": name, "conclusion": "success"}
            for name in authorize_module.REQUIRED_CHECK_NAMES
        ]

        authorize_module.authorize(sha, runs, "main", cwd=git_repo)

    def test_a_single_missing_required_check_among_an_otherwise_full_set_is_refused(
        self, git_repo
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        names = sorted(authorize_module.REQUIRED_CHECK_NAMES)
        dropped, remaining = names[0], names[1:]
        runs = [{"name": name, "conclusion": "success"} for name in remaining]

        with pytest.raises(
            authorize_module.AuthorizationError, match=re.escape(dropped)
        ):
            authorize_module.authorize(sha, runs, "main", cwd=git_repo)

    def test_authorize_refuses_a_failed_chart_check_when_every_other_check_passes(
        self, git_repo
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        chart_check = "Chart (lint + template + kubeconform)"
        runs = [
            {
                "name": name,
                "conclusion": "failure" if name == chart_check else "success",
            }
            for name in authorize_module.REQUIRED_CHECK_NAMES
        ]

        with pytest.raises(
            authorize_module.AuthorizationError, match=re.escape(chart_check)
        ):
            authorize_module.authorize(sha, runs, "main", cwd=git_repo)


class TestFetchCheckRuns:
    """`gh api` must be pinned to GET (issue #732, defect 1).

    `gh api` defaults to GET but switches to POST as soon as any `-f`/`-F`
    flag is present, and `POST /repos/{owner}/{repo}/commits/{sha}/check-runs`
    does not exist. Verified live against curie-eng/curie on 2026-07-21 at
    commit 276774ff: the `-f`-only form returned
    `{"message": "Not Found", "status": "404"}`, while adding `-X GET`
    returned `{"total_count": 42, ...}`. Mocking `gh` here is correct: GitHub
    is an external service.
    """

    @staticmethod
    def _capture(monkeypatch, payload: dict) -> list:
        captured: list = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(authorize_module.subprocess, "run", fake_run)
        return captured

    def test_check_runs_are_fetched_with_an_explicit_get(self, monkeypatch):
        payload = {"total_count": 1, "check_runs": [CHECK_RUNS_ALL_GREEN[0]]}
        captured = self._capture(monkeypatch, payload)

        runs = authorize_module.fetch_check_runs("deadbeef", "curie-eng/curie")

        argv = captured[0]
        endpoint = "repos/curie-eng/curie/commits/deadbeef/check-runs"
        assert "-X" in argv
        assert argv[argv.index("-X") + 1] == "GET"
        assert argv.index("-X") < argv.index(endpoint)
        assert "-f" in argv
        assert argv[argv.index("-f") + 1] == "per_page=100"
        assert runs == [CHECK_RUNS_ALL_GREEN[0]]


class TestFetchCheckRunsPagination:
    """The check-runs endpoint's default page size is 30, and a real commit
    on this repo has been measured with several dozen check-runs across its
    workflows (issue #733) -- comfortably past that default and past what a
    single `per_page=100` page happened to cover historically. These tests
    drive `fetch_check_runs`'s own pagination loop (not a stubbed
    single-response mock) to prove it walks every page, and that a required
    check which only fails or is only missing on a later page still causes a
    refusal rather than being silently dropped.
    """

    @staticmethod
    def _paged_fake_run(pages: dict, total_count: int):
        def fake_run(argv, **kwargs):
            page_arg = next(
                arg for arg in argv if isinstance(arg, str) and arg.startswith("page=")
            )
            page = int(page_arg.split("=", 1)[1])
            payload = {"total_count": total_count, "check_runs": pages.get(page, [])}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        return fake_run

    def test_collects_every_page_in_order(self, monkeypatch):
        pages = {
            1: [
                {"name": "CI", "conclusion": "success"},
                {"name": "Unrelated", "conclusion": "success"},
            ],
            2: [{"name": "CodeQL", "conclusion": "neutral"}],
        }
        monkeypatch.setattr(
            authorize_module.subprocess, "run", self._paged_fake_run(pages, total_count=3)
        )

        runs = authorize_module.fetch_check_runs("deadbeef", "curie-eng/curie", per_page=2)

        assert [run["name"] for run in runs] == ["CI", "Unrelated", "CodeQL"]

    def test_a_required_check_failing_only_on_a_later_page_still_refuses(self, monkeypatch):
        pages = {
            1: [
                {"name": "CI", "conclusion": "success"},
                {"name": "Unrelated", "conclusion": "success"},
            ],
            2: [{"name": "CodeQL", "conclusion": "failure"}],
        }
        monkeypatch.setattr(
            authorize_module.subprocess, "run", self._paged_fake_run(pages, total_count=3)
        )

        runs = authorize_module.fetch_check_runs("deadbeef", "curie-eng/curie", per_page=2)

        assert len(runs) == 3
        assert authorize_module.missing_required_checks(runs, TEST_REQUIRED_NAMES) == {
            "CodeQL"
        }

    def test_a_required_check_only_present_on_a_later_page_still_authorizes(
        self, git_repo, monkeypatch
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        pages = {
            1: [{"name": "CI", "conclusion": "success"}],
            2: [{"name": "CodeQL", "conclusion": "success"}],
        }
        # Scope the `gh api` stub to the fetch call only -- `authorize()` below
        # also shells out to real `git merge-base` via the same
        # `subprocess.run`, which must not be intercepted by this fake.
        with monkeypatch.context() as page_fetch:
            page_fetch.setattr(
                authorize_module.subprocess, "run", self._paged_fake_run(pages, total_count=2)
            )
            runs = authorize_module.fetch_check_runs(
                "deadbeef", "curie-eng/curie", per_page=1
            )

        authorize_module.authorize(
            sha, runs, "main", cwd=git_repo, required_names=TEST_REQUIRED_NAMES
        )

    def test_stops_when_a_page_reports_nothing_even_if_total_count_implied_more(
        self, monkeypatch
    ):
        # A stale/wrong total_count must not spin the loop forever.
        pages = {1: [{"name": "CI", "conclusion": "success"}], 2: []}
        monkeypatch.setattr(
            authorize_module.subprocess, "run", self._paged_fake_run(pages, total_count=5)
        )

        runs = authorize_module.fetch_check_runs("deadbeef", "curie-eng/curie", per_page=1)

        assert runs == [{"name": "CI", "conclusion": "success"}]


class TestExcludeCurrentWorkflowRun:
    """The gate is itself a check-run on the tagged SHA (issue #732, defect 2)."""

    def test_current_run_entries_are_dropped_and_others_survive(self):
        other = check_run("CI", "success", OTHER_RUN_ID, "88573600001")

        remaining = authorize_module.exclude_current_workflow_run(
            [GATE_OWN_IN_PROGRESS, other], CURRENT_RUN_ID
        )

        assert remaining == [other]

    def test_falsy_run_id_leaves_the_list_untouched(self):
        runs = [GATE_OWN_IN_PROGRESS, check_run("CI", "success", OTHER_RUN_ID, "1")]

        assert authorize_module.exclude_current_workflow_run(runs, None) == runs
        assert authorize_module.exclude_current_workflow_run(runs, "") == runs

    def test_entries_without_details_url_are_never_dropped(self):
        external = {"name": "External Check", "status": "completed", "conclusion": "success"}

        remaining = authorize_module.exclude_current_workflow_run(
            [GATE_OWN_IN_PROGRESS, external], CURRENT_RUN_ID
        )

        assert remaining == [external]


class TestAuthorizeExcludesCurrentRun:
    def test_gate_own_in_progress_entry_does_not_block_authorization(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "neutral", OTHER_RUN_ID, "88573600002"),
        ]

        authorize_module.authorize(
            sha,
            runs,
            "main",
            cwd=git_repo,
            exclude_run_id=CURRENT_RUN_ID,
            required_names=TEST_REQUIRED_NAMES,
        )

    def test_unrelated_check_present_does_not_block_when_required_checks_are_green(
        self, git_repo
    ):
        # New semantics (issue #733): only the required names are asserted --
        # an unrelated check-run, however incomplete, has no bearing.
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "neutral", OTHER_RUN_ID, "88573600002"),
            check_run("Integration Tests", None, OTHER_RUN_ID, "88573600003"),
        ]

        authorize_module.authorize(
            sha,
            runs,
            "main",
            cwd=git_repo,
            exclude_run_id=CURRENT_RUN_ID,
            required_names=TEST_REQUIRED_NAMES,
        )

    def test_required_check_still_in_progress_is_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", None, OTHER_RUN_ID, "88573600002"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="required check-run"):
            authorize_module.authorize(
                sha,
                runs,
                "main",
                cwd=git_repo,
                exclude_run_id=CURRENT_RUN_ID,
                required_names=TEST_REQUIRED_NAMES,
            )

    def test_required_check_that_concluded_failure_is_refused(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "failure", OTHER_RUN_ID, "88573600004"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="required check-run"):
            authorize_module.authorize(
                sha,
                runs,
                "main",
                cwd=git_repo,
                exclude_run_id=CURRENT_RUN_ID,
                required_names=TEST_REQUIRED_NAMES,
            )

    def test_only_current_run_checks_is_refused(self, git_repo):
        # Nothing survives filtering, and absence of checks is not evidence
        # they passed.
        sha = run_git(git_repo, "rev-parse", "HEAD")
        runs = [
            GATE_OWN_IN_PROGRESS,
            check_run("authorize-release setup", "success", CURRENT_RUN_ID, "88573652087"),
        ]

        with pytest.raises(authorize_module.AuthorizationError, match="required check-run"):
            authorize_module.authorize(
                sha,
                runs,
                "main",
                cwd=git_repo,
                exclude_run_id=CURRENT_RUN_ID,
                required_names=TEST_REQUIRED_NAMES,
            )


class TestMain:
    """`main()` must thread `GITHUB_RUN_ID` into `authorize()` as
    `exclude_run_id` (issue #732, defect 2).

    `main()` never passes `required_names` through explicitly, so it always
    resolves the module-level `REQUIRED_CHECK_NAMES` at call time; these tests
    monkeypatch that constant to the small `TEST_REQUIRED_NAMES` set so the
    fixtures stay independent of the production ci.yaml job list.

    Note on the required-check allowlist (issue #733): the gate's own
    check-run is always named after its job (e.g. `authorize-release`), never
    after a ci.yaml job, so it can never itself satisfy or block a required
    name -- unlike the old "every present check-run must pass" rule, an
    unfiltered self-entry sitting in the list with `conclusion: null` no
    longer affects the outcome at all. What still matters, and what these
    tests cover, is that a *wrong* run id can incorrectly filter out a
    legitimate required check-run (mistaking another run's job for this one),
    which must still refuse.

    `fetch_check_runs` is stubbed so no network call happens; `authorize()`
    runs for real against `git_repo`, so `main()` is run with that repo as
    the working directory (`main()` calls `authorize()` without a `cwd`).
    """

    @staticmethod
    def _stub_fetch_check_runs(monkeypatch, runs: list[dict]) -> None:
        monkeypatch.setattr(
            authorize_module, "fetch_check_runs", lambda sha, repo: runs
        )

    @staticmethod
    def _use_test_required_names(monkeypatch) -> None:
        monkeypatch.setattr(authorize_module, "REQUIRED_CHECK_NAMES", TEST_REQUIRED_NAMES)

    @staticmethod
    def _runs_with_gate_own_in_progress() -> list[dict]:
        return [
            GATE_OWN_IN_PROGRESS,
            check_run("CI", "success", OTHER_RUN_ID, "88573600001"),
            check_run("CodeQL", "neutral", OTHER_RUN_ID, "88573600002"),
        ]

    def test_main_authorizes_when_github_run_id_excludes_its_own_check(
        self, git_repo, monkeypatch
    ):
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._use_test_required_names(monkeypatch)
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", CURRENT_RUN_ID)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/curie", "--main-ref", "main"]
        )

        assert exit_code == 0

    def test_main_still_authorizes_when_github_run_id_is_absent_and_required_checks_are_green(
        self, git_repo, monkeypatch
    ):
        # The gate's own check-run is never itself a required name, so
        # leaving it unfiltered (no run id to exclude by) has no bearing on
        # whether the real required checks (CI, CodeQL here) are satisfied.
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._use_test_required_names(monkeypatch)
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/curie", "--main-ref", "main"]
        )

        assert exit_code == 0

    def test_main_refuses_when_github_run_id_is_a_different_run(
        self, git_repo, monkeypatch
    ):
        # The fixture's real "CI"/"CodeQL" entries are marked as belonging to
        # OTHER_RUN_ID; passing that value as GITHUB_RUN_ID makes
        # `exclude_current_workflow_run` mistake them for this run's own and
        # strip them out, leaving only the gate's unrelated in-progress entry.
        # A wrong run id over-excluding legitimate required checks must still
        # refuse, not slip through.
        sha = run_git(git_repo, "rev-parse", "HEAD")
        self._use_test_required_names(monkeypatch)
        self._stub_fetch_check_runs(monkeypatch, self._runs_with_gate_own_in_progress())
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", OTHER_RUN_ID)

        exit_code = authorize_module.main(
            [sha, "--repo", "curie-eng/curie", "--main-ref", "main"]
        )

        assert exit_code == 1


class TestMainLookupFailures:
    """A failed check-run lookup must refuse legibly, not traceback (#732).

    Before this, `main()` caught only `AuthorizationError`, so every failure
    inside `fetch_check_runs` escaped as an unhandled traceback. The exit code
    was already 1, so the gate did fail closed; what was missing was any way
    for an operator to tell an unauthorized tag from a lookup that never
    completed -- which is exactly why the `gh api` POST/GET defect read as an
    opaque crash. Each path below must still return 1.
    """

    @staticmethod
    def _stub_fetch_raising(monkeypatch, exc: BaseException) -> None:
        def raising(sha, repo):
            raise exc

        monkeypatch.setattr(authorize_module, "fetch_check_runs", raising)

    @staticmethod
    def _run_main(git_repo, monkeypatch) -> int:
        sha = run_git(git_repo, "rev-parse", "HEAD")
        monkeypatch.chdir(git_repo)
        monkeypatch.setenv("GITHUB_RUN_ID", CURRENT_RUN_ID)
        return authorize_module.main(
            [sha, "--repo", "curie-eng/curie", "--main-ref", "main"]
        )

    def test_gh_api_failure_refuses_with_a_message_naming_the_lookup(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(
            monkeypatch,
            subprocess.CalledProcessError(
                1,
                ["gh", "api", "-X", "GET", "repos/curie-eng/curie/commits/x/check-runs"],
                stderr="gh: Not Found (HTTP 404)",
            ),
        )

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "ERROR: could not retrieve check-runs" in stderr
        assert "gh: Not Found (HTTP 404)" in stderr

    def test_unparseable_response_refuses_with_a_message_naming_the_lookup(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(
            monkeypatch, json.JSONDecodeError("Expecting value", "not json", 0)
        )

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        assert "ERROR: could not retrieve check-runs" in capsys.readouterr().err

    def test_payload_without_check_runs_key_refuses_with_a_message(
        self, git_repo, monkeypatch, capsys
    ):
        self._stub_fetch_raising(monkeypatch, KeyError("check_runs"))

        exit_code = self._run_main(git_repo, monkeypatch)

        assert exit_code == 1
        assert "ERROR: could not retrieve check-runs" in capsys.readouterr().err

    def test_lookup_failure_is_distinguishable_from_an_unauthorized_tag(
        self, git_repo, monkeypatch, capsys
    ):
        # The unauthorized-tag wording must not appear on the lookup path, or
        # an operator cannot tell the two refusals apart.
        monkeypatch.setattr(authorize_module, "REQUIRED_CHECK_NAMES", TEST_REQUIRED_NAMES)
        self._stub_fetch_raising(monkeypatch, KeyError("check_runs"))

        assert self._run_main(git_repo, monkeypatch) == 1
        lookup_stderr = capsys.readouterr().err

        monkeypatch.setattr(
            authorize_module, "fetch_check_runs", lambda sha, repo: CHECK_RUNS_ONE_FAILED
        )
        assert self._run_main(git_repo, monkeypatch) == 1
        refusal_stderr = capsys.readouterr().err

        assert "could not retrieve check-runs" in lookup_stderr
        assert "could not retrieve check-runs" not in refusal_stderr
        assert "required check-run" in refusal_stderr


CI_YAML = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
HELM_CI_YAML = REPO_ROOT / ".github" / "workflows" / "helm-ci.yaml"

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_]+)\s*\}\}")


def ci_job_check_run_names() -> set[str]:
    """The concrete check-run names ci.yaml's jobs produce (issue #811).

    Parses the real workflow rather than any list derived from
    `REQUIRED_CHECK_NAMES` -- deriving the expected set from the constant
    would recreate the very drift-blindness issue #811 is about. Each job's
    check-run name is its `name:` field; a matrixed job whose name interpolates
    `${{ matrix.<key> }}` and that declares `strategy.matrix.include` expands
    to one concrete name per include row, substituting that row's `<key>`
    value. The substitution is general over the matrix key (regex, not a
    literal `matrix.name`), so a future job that matrixes on a different key
    is handled without editing this helper. A job with no `name:` is skipped.
    """
    doc = yaml.safe_load(CI_YAML.read_text())
    names: set[str] = set()
    for job in doc["jobs"].values():
        name = job.get("name")
        if not name:
            continue
        ref = _MATRIX_REF.search(name)
        include = ((job.get("strategy") or {}).get("matrix") or {}).get("include")
        if ref and include:
            for entry in include:
                names.add(_MATRIX_REF.sub(lambda m, entry=entry: str(entry[m.group(1)]), name))
        else:
            names.add(name)
    return names


def helm_ci_job_check_run_names() -> set[str]:
    doc = yaml.safe_load(HELM_CI_YAML.read_text())
    return {job["name"] for job in doc["jobs"].values() if job.get("name")}


class TestRequiredNamesMatchCiWorkflows:
    """`REQUIRED_CHECK_NAMES` must not drift from CI workflow job names (#811).

    The gate is fail-closed: a required name that no CI workflow job produces can
    never appear among a commit's real check-runs, so it is reported missing on
    every otherwise-legitimate commit and blocks the release. Conversely a
    stale name masks the loss of the check it was meant to assert. This pins the
    constant as a subset of the concrete check-run names the current CI workflows
    actually emit, parsed live (never derived from the constant itself).

    So renaming or splitting a required job in a CI workflow without updating
    `REQUIRED_CHECK_NAMES` fails here (with the drifted names listed), rather
    than silently dropping a release gate.
    """

    def test_every_required_name_is_a_real_ci_workflow_check_run_name(self):
        ci_names = ci_job_check_run_names() | helm_ci_job_check_run_names()
        stale = authorize_module.REQUIRED_CHECK_NAMES - ci_names

        assert authorize_module.REQUIRED_CHECK_NAMES <= ci_names, (
            "REQUIRED_CHECK_NAMES has drifted from the CI workflows -- these required "
            f"names match no current job check-run: {sorted(stale)}"
        )


class TestHelmCiWorkflowTriggers:
    """Pin the deliberate push and pull request trigger asymmetry."""

    def test_push_trigger_is_unfiltered_for_releasable_branches(self):
        doc = yaml.safe_load(HELM_CI_YAML.read_text())
        triggers = doc[True]

        assert triggers["push"]["branches"] == ["main", "next"]
        assert "paths" not in triggers["push"]
        assert "paths-ignore" not in triggers["push"]
        assert triggers["pull_request"]["branches"] == ["main", "next"]
        assert triggers["pull_request"]["paths"] == [
            "charts/curie/**",
            ".github/workflows/helm-ci.yaml",
        ]


class TestMixedPassFailRequiredCheck:
    """A required name with any non-passing run is not satisfied (issue #811).

    The set logic only tracks names that have at least one *passing* run, so a
    required check that ran twice -- once green, once red (a re-run that failed)
    -- has its name in the passing set and is silently treated as satisfied.
    For a fail-closed release gate that masks a genuinely failing required
    check. These use the `required_names` override so they exercise the logic
    independent of the production constant.
    """

    MIXED_CI = [
        {"name": "CI", "conclusion": "success"},
        {"name": "CI", "conclusion": "failure"},  # a re-run that failed
        {"name": "CodeQL", "conclusion": "success"},
    ]

    def test_a_required_name_with_a_failing_run_is_reported_missing(self):
        assert "CI" in authorize_module.missing_required_checks(
            self.MIXED_CI, TEST_REQUIRED_NAMES
        )

    def test_a_required_name_with_a_failing_run_is_not_satisfied(self):
        assert authorize_module.missing_required_checks(
            self.MIXED_CI, TEST_REQUIRED_NAMES
        )

    def test_a_single_passing_run_with_no_failing_run_is_satisfied(self):
        # Positive boundary: the fix must not over-reject a clean pass.
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CodeQL", "conclusion": "success"},
        ]

        assert authorize_module.missing_required_checks(runs, TEST_REQUIRED_NAMES) == set()

    def test_multiple_all_passing_runs_stay_satisfied(self):
        # Positive boundary: several entries for a required name, all passing.
        runs = [
            {"name": "CI", "conclusion": "success"},
            {"name": "CI", "conclusion": "neutral"},
            {"name": "CodeQL", "conclusion": "success"},
        ]

        assert authorize_module.missing_required_checks(runs, TEST_REQUIRED_NAMES) == set()

    def test_authorize_refuses_a_mixed_pass_fail_required_check(self, git_repo):
        sha = run_git(git_repo, "rev-parse", "HEAD")

        with pytest.raises(
            authorize_module.AuthorizationError, match="required check-run"
        ):
            authorize_module.authorize(
                sha,
                self.MIXED_CI,
                "main",
                cwd=git_repo,
                required_names=TEST_REQUIRED_NAMES,
            )
