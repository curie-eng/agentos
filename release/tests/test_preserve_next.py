"""Contract tests for the next-branch preservation preflight (issue #2090).

Merging the ordinary `next` -> `main` release PR deleted protected `next`
because the repository keeps `delete_branch_on_merge` enabled (so short-lived
task branches still clean up) and the deletion ruleset that covers `next` lists
bypass actors. GitHub then auto-deletes the PR head, and a bypass-capable
merger is not stopped by that ruleset.

These tests drive the gate against constructed GitHub payloads, the same split
`release/authorize.py` uses: the negative case is an ordinary pytest assertion,
not a destructive merge of the live `next` branch. Live read-only evidence for
the incident (PR #2085 `head_ref_deleted` six seconds after merge; repository
`delete_branch_on_merge=true`; ruleset 18711121 covers `refs/heads/next` with a
`deletion` rule and a bypass actor) is recorded in the issue and the pull
request, not copied as real identifiers here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "release" / "preserve_next.py"
CI_YAML = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
RELEASE_YAML = REPO_ROOT / ".github" / "workflows" / "release.yaml"


def load_module():
    spec = importlib.util.spec_from_file_location("release_preserve_next", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses read sys.modules[cls.__module__] while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preserve_next = load_module()


def ruleset_payload(
    *,
    ruleset_id: int = 1,
    name: str = "Default",
    enforcement: str = "active",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    rules: list[dict] | None = None,
    bypass_actors: list[dict] | None = None,
    omit_bypass_actors: bool = False,
) -> dict:
    payload = {
        "id": ruleset_id,
        "name": name,
        "enforcement": enforcement,
        "conditions": {
            "ref_name": {
                "include": include
                if include is not None
                else ["~DEFAULT_BRANCH", "refs/heads/next"],
                "exclude": exclude if exclude is not None else [],
            }
        },
        "rules": rules if rules is not None else [{"type": "deletion"}],
    }
    if not omit_bypass_actors:
        payload["bypass_actors"] = (
            bypass_actors
            if bypass_actors is not None
            else [
                {
                    "actor_id": 1001,
                    "actor_type": "User",
                    "bypass_mode": "always",
                }
            ]
        )
    return payload


def repo_payload(*, delete_branch_on_merge: bool = True, default_branch: str = "main") -> dict:
    return {
        "delete_branch_on_merge": delete_branch_on_merge,
        "default_branch": default_branch,
        "name": "curie",
    }


def pr_event(head: str, base: str = "main") -> dict:
    return {"action": "opened", "pull_request": {"head": {"ref": head}, "base": {"ref": base}}}


def push_event(ref: str = "refs/heads/main") -> dict:
    return {"ref": ref}


def release_train_workflow(branches: list[str] | None = None) -> dict:
    return {"on": {"push": {"branches": branches if branches is not None else ["main", "next"]}}}


def settings_from(payload: dict):
    return preserve_next.parse_repo_settings(payload)


def rulesets_from(*payloads: dict):
    return [preserve_next.parse_ruleset(payload) for payload in payloads]


# Snapshot of the unsafe ordinary release path: auto-delete is on, `next` is
# the PR head, and the deletion ruleset that covers `next` is bypassable.
UNSAFE_REPO = repo_payload()
UNSAFE_RULESET = ruleset_payload()
UNSAFE_WORKFLOW = release_train_workflow()


class TestPredictHeadBranchDeletedAfterMerge:
    """Isolated GitHub merge/deletion fixture (issue #2090).

    GitHub auto-deletes a merged PR head when `delete_branch_on_merge` is
    true, unless a ruleset deletion rule covers that head and the merger
    cannot bypass it. Documented at
    https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches
    Observed on this repository's PR #2085: `merged` at 2026-08-29T22:27:46Z,
    `head_ref_deleted` for `next` at 2026-08-29T22:27:52Z.
    """

    def test_ordinary_next_to_main_merge_deletes_next_when_deletion_rule_is_bypassable(
        self,
    ):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(UNSAFE_RULESET)

        assert preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )
        assert not preserve_next.next_survives_auto_delete_on_merge(
            settings, rulesets, "next"
        )

    def test_task_branch_head_is_deleted_and_next_is_preserved(self):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(UNSAFE_RULESET)

        assert preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "task/release-v0.8.0"
        )
        assert preserve_next.next_survives_auto_delete_on_merge(
            settings, rulesets, "task/release-v0.8.0"
        )

    def test_empty_bypass_deletion_ruleset_preserves_next(self):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(ruleset_payload(bypass_actors=[]))

        assert not preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )
        assert preserve_next.next_survives_auto_delete_on_merge(
            settings, rulesets, "next"
        )

    def test_auto_delete_disabled_preserves_next(self):
        settings = settings_from(repo_payload(delete_branch_on_merge=False))
        rulesets = rulesets_from(UNSAFE_RULESET)

        assert not preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )
        assert preserve_next.next_survives_auto_delete_on_merge(
            settings, rulesets, "next"
        )

    def test_missing_bypass_actors_does_not_count_as_protection(self):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(ruleset_payload(omit_bypass_actors=True))

        assert preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )

    def test_evaluate_enforcement_does_not_count_as_protection(self):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(
            ruleset_payload(enforcement="evaluate", bypass_actors=[])
        )

        assert preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )

    def test_ruleset_that_does_not_cover_next_does_not_protect_it(self):
        settings = settings_from(UNSAFE_REPO)
        rulesets = rulesets_from(
            ruleset_payload(include=["~DEFAULT_BRANCH"], bypass_actors=[])
        )

        assert preserve_next.predict_head_branch_deleted_after_merge(
            settings, rulesets, "next"
        )


class TestEvaluatePreflight:
    def test_refuses_ordinary_next_to_main_release_merge(self):
        with pytest.raises(preserve_next.PreserveNextError) as exc_info:
            preserve_next.evaluate(
                event=pr_event("next"),
                settings=settings_from(UNSAFE_REPO),
                rulesets=rulesets_from(UNSAFE_RULESET),
                workflow=UNSAFE_WORKFLOW,
            )

        message = str(exc_info.value)
        assert "next" in message
        assert "delete_branch_on_merge" in message
        assert "bypass" in message

    def test_allows_a_short_lived_release_task_branch(self):
        preserve_next.evaluate(
            event=pr_event("task/release-v0.8.0"),
            settings=settings_from(UNSAFE_REPO),
            rulesets=rulesets_from(UNSAFE_RULESET),
            workflow=UNSAFE_WORKFLOW,
        )

    def test_allows_next_to_main_when_deletion_ruleset_has_no_bypass(self):
        preserve_next.evaluate(
            event=pr_event("next"),
            settings=settings_from(UNSAFE_REPO),
            rulesets=rulesets_from(ruleset_payload(bypass_actors=[])),
            workflow=UNSAFE_WORKFLOW,
        )

    def test_allows_next_to_main_when_auto_delete_is_disabled(self):
        preserve_next.evaluate(
            event=pr_event("next"),
            settings=settings_from(repo_payload(delete_branch_on_merge=False)),
            rulesets=rulesets_from(UNSAFE_RULESET),
            workflow=UNSAFE_WORKFLOW,
        )

    def test_allows_next_to_main_after_the_documented_retirement_sequence(self):
        preserve_next.evaluate(
            event=pr_event("next"),
            settings=settings_from(UNSAFE_REPO),
            rulesets=rulesets_from(UNSAFE_RULESET),
            workflow=release_train_workflow(["main"]),
        )

    def test_push_events_are_not_release_merges(self):
        preserve_next.evaluate(
            event=push_event(),
            settings=settings_from(UNSAFE_REPO),
            rulesets=rulesets_from(UNSAFE_RULESET),
            workflow=UNSAFE_WORKFLOW,
        )

    def test_next_into_next_is_not_the_ordinary_release_merge(self):
        preserve_next.evaluate(
            event=pr_event("next", base="next"),
            settings=settings_from(UNSAFE_REPO),
            rulesets=rulesets_from(UNSAFE_RULESET),
            workflow=UNSAFE_WORKFLOW,
        )


class TestReleaseTrainDetection:
    def test_committed_release_workflow_still_treats_next_as_a_release_train_branch(
        self,
    ):
        workflow = yaml.load(RELEASE_YAML.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

        assert preserve_next.next_is_release_train_branch(workflow)
        assert "next" in workflow["on"]["push"]["branches"]

    def test_a_workflow_without_next_is_the_retirement_state(self):
        assert not preserve_next.next_is_release_train_branch(
            release_train_workflow(["main"])
        )


class TestFetchUsesGet:
    """`gh api` must be pinned to GET, matching issue #732 on authorize.py."""

    @staticmethod
    def _capture(monkeypatch, responses: dict[str, dict]) -> list:
        captured: list = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            endpoint = next(
                arg for arg in argv if isinstance(arg, str) and arg.startswith("repos/")
            )
            payload = responses[endpoint]
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        monkeypatch.setattr(preserve_next.subprocess, "run", fake_run)
        return captured

    def test_repo_settings_are_fetched_with_an_explicit_get(self, monkeypatch):
        captured = self._capture(
            monkeypatch, {"repos/curie-eng/curie": repo_payload()}
        )

        settings = preserve_next.fetch_repo_settings("curie-eng/curie")

        argv = captured[0]
        assert argv[argv.index("-X") + 1] == "GET"
        assert settings.delete_branch_on_merge is True

    def test_gh_env_disables_color_so_the_payload_stays_json(self, monkeypatch):
        captured_env: list[dict] = []

        def fake_run(argv, **kwargs):
            captured_env.append(kwargs.get("env") or {})
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(repo_payload()), stderr=""
            )

        monkeypatch.setattr(preserve_next.subprocess, "run", fake_run)
        monkeypatch.setenv("CLICOLOR_FORCE", "1")

        preserve_next.fetch_repo_settings("curie-eng/curie")

        env = captured_env[0]
        assert env["NO_COLOR"] == "1"
        assert env["GH_FORCE_TTY"] == "0"
        assert "CLICOLOR_FORCE" not in env

    def test_rulesets_are_re_fetched_for_bypass_actors(self, monkeypatch):
        list_payload = [{"id": 1, "name": "Default", "enforcement": "active"}]
        detail = ruleset_payload()
        captured = self._capture(
            monkeypatch,
            {
                "repos/curie-eng/curie/rulesets": list_payload,
                "repos/curie-eng/curie/rulesets/1": detail,
            },
        )

        rulesets = preserve_next.fetch_rulesets("curie-eng/curie")

        endpoints = [
            next(arg for arg in argv if isinstance(arg, str) and arg.startswith("repos/"))
            for argv in captured
        ]
        assert endpoints == [
            "repos/curie-eng/curie/rulesets",
            "repos/curie-eng/curie/rulesets/1",
        ]
        assert all(argv[argv.index("-X") + 1] == "GET" for argv in captured)
        assert rulesets[0].bypass_actors is not None
        assert len(rulesets[0].bypass_actors) == 1


class TestMain:
    @staticmethod
    def _stub_live(monkeypatch, *, repo: dict, rulesets: list[dict]) -> None:
        monkeypatch.setattr(
            preserve_next,
            "fetch_repo_settings",
            lambda _repo: preserve_next.parse_repo_settings(repo),
        )
        monkeypatch.setattr(
            preserve_next,
            "fetch_rulesets",
            lambda _repo: [preserve_next.parse_ruleset(item) for item in rulesets],
        )
        monkeypatch.setattr(
            preserve_next,
            "load_release_workflow",
            lambda: UNSAFE_WORKFLOW,
        )

    def test_refuses_an_ordinary_next_to_main_pr(self, tmp_path, monkeypatch, capsys):
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(pr_event("next")), encoding="utf-8")
        self._stub_live(monkeypatch, repo=UNSAFE_REPO, rulesets=[UNSAFE_RULESET])

        exit_code = preserve_next.main(
            ["--repo", "curie-eng/curie", "--event", str(event_path)]
        )

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "ERROR:" in err
        assert "delete_branch_on_merge" in err

    def test_allows_a_task_branch_pr_without_requiring_the_admin_setting_change(
        self, tmp_path, monkeypatch, capsys
    ):
        event_path = tmp_path / "event.json"
        event_path.write_text(
            json.dumps(pr_event("task/2090-preserve-next-branch")), encoding="utf-8"
        )
        self._stub_live(monkeypatch, repo=UNSAFE_REPO, rulesets=[UNSAFE_RULESET])

        exit_code = preserve_next.main(
            ["--repo", "curie-eng/curie", "--event", str(event_path)]
        )

        assert exit_code == 0
        assert "OK:" in capsys.readouterr().out

    def test_operator_invocation_without_an_event_checks_the_ordinary_release_path(
        self, monkeypatch, capsys
    ):
        self._stub_live(monkeypatch, repo=UNSAFE_REPO, rulesets=[UNSAFE_RULESET])
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)

        exit_code = preserve_next.main(["--repo", "curie-eng/curie"])

        assert exit_code == 1
        assert "delete_branch_on_merge" in capsys.readouterr().err

    def test_lookup_failure_refuses_closed(self, tmp_path, monkeypatch, capsys):
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(pr_event("next")), encoding="utf-8")
        monkeypatch.setattr(
            preserve_next,
            "fetch_repo_settings",
            lambda _repo: (_ for _ in ()).throw(
                subprocess.CalledProcessError(
                    1,
                    ["gh", "api", "-X", "GET", "repos/curie-eng/curie"],
                    stderr="gh: Not Found (HTTP 404)",
                )
            ),
        )
        monkeypatch.setattr(
            preserve_next, "load_release_workflow", lambda: UNSAFE_WORKFLOW
        )

        exit_code = preserve_next.main(
            ["--repo", "curie-eng/curie", "--event", str(event_path)]
        )

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "could not retrieve" in err
        assert "gh: Not Found (HTTP 404)" in err


class TestCiWorkflowContract:
    """The consumer path is the CI job, not the helper sitting unused."""

    def test_preserve_next_job_invokes_the_script_and_never_skips(self):
        workflow = yaml.safe_load(CI_YAML.read_text(encoding="utf-8"))
        job = workflow["jobs"]["preserve-next"]

        assert "if" not in job
        assert job.get("continue-on-error") is not True
        runs = [step.get("run", "") for step in job["steps"]]
        assert any("release/preserve_next.py" in run for run in runs)
        assert any("--repo" in run and "GITHUB_REPOSITORY" in run for run in runs)

    def test_preserve_next_job_supplies_a_token_for_gh_api(self):
        workflow = yaml.safe_load(CI_YAML.read_text(encoding="utf-8"))
        job = workflow["jobs"]["preserve-next"]
        step = next(
            item
            for item in job["steps"]
            if "release/preserve_next.py" in item.get("run", "")
        )
        env = step.get("env") or job.get("env") or {}
        assert env.get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}"
