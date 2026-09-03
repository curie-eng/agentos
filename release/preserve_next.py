#!/usr/bin/env python3
"""Refuse an ordinary next-to-main release merge that would delete `next`.

Issue #2090: merging a pull request whose head is the protected `next` branch
into `main` deleted `next` because the repository keeps
`delete_branch_on_merge` enabled (so short-lived task branches still clean up)
and the deletion ruleset that covers `next` lists bypass actors. GitHub then
auto-deletes the PR head; a bypass-capable merger is not stopped by that
ruleset.

This script is the fail-closed preflight that must run before that ordinary
path is merged. It allows:

  * a short-lived `task/release-*` (or any non-`next`) head, which is the
    repository-owned way to keep task-branch cleanup without deleting `next`;
  * an active deletion ruleset that covers `next` and has an empty bypass
    list, which is the durable GitHub configuration (admin-only to apply);
  * `delete_branch_on_merge` turned off (loses task-branch cleanup);
  * the documented retirement sequence, once `next` is no longer a release
    train trigger branch.

Anything else, including a lookup that did not complete, is a refusal. The
predicate lives in separately-testable functions so the unsafe ordinary path
is an ordinary pytest assertion against a constructed GitHub fixture, not a
destructive merge of the live `next` branch. Only `main()` needs the network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

NEXT_BRANCH = "next"
DEFAULT_BASE = "main"
RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yaml"
_PUSH_BRANCHES = re.compile(r"^\s+branches:\s*\[([^\]]*)\]", re.MULTILINE)


class PreserveNextError(Exception):
    """An ordinary release merge would delete the protected next branch."""


@dataclass(frozen=True)
class BypassActor:
    actor_id: object
    actor_type: str
    bypass_mode: str


@dataclass(frozen=True)
class Ruleset:
    id: int
    name: str
    enforcement: str
    include_refs: tuple[str, ...]
    exclude_refs: tuple[str, ...]
    rule_types: tuple[str, ...]
    bypass_actors: tuple[BypassActor, ...] | None


@dataclass(frozen=True)
class RepoSettings:
    delete_branch_on_merge: bool
    default_branch: str


@dataclass(frozen=True)
class PullRequestRefs:
    head_ref: str
    base_ref: str


def parse_repo_settings(payload: dict[str, object]) -> RepoSettings:
    delete = payload.get("delete_branch_on_merge")
    if not isinstance(delete, bool):
        raise PreserveNextError(
            "repository payload is missing a boolean delete_branch_on_merge"
        )
    default_branch = payload.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise PreserveNextError("repository payload is missing default_branch")
    return RepoSettings(
        delete_branch_on_merge=delete, default_branch=default_branch
    )


def parse_ruleset(payload: dict[str, object]) -> Ruleset:
    conditions = payload.get("conditions")
    ref_name: dict[str, object] = {}
    if isinstance(conditions, dict):
        raw_ref = conditions.get("ref_name")
        if isinstance(raw_ref, dict):
            ref_name = raw_ref
    include = ref_name.get("include")
    exclude = ref_name.get("exclude")
    rules = payload.get("rules")
    rule_types: list[str] = []
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict) and isinstance(rule.get("type"), str):
                rule_types.append(str(rule["type"]))
    bypass: tuple[BypassActor, ...] | None
    if "bypass_actors" not in payload:
        bypass = None
    else:
        raw_bypass = payload.get("bypass_actors")
        actors: list[BypassActor] = []
        if isinstance(raw_bypass, list):
            for item in raw_bypass:
                if not isinstance(item, dict):
                    continue
                actors.append(
                    BypassActor(
                        actor_id=item.get("actor_id"),
                        actor_type=str(item.get("actor_type") or ""),
                        bypass_mode=str(item.get("bypass_mode") or ""),
                    )
                )
        bypass = tuple(actors)
    return Ruleset(
        id=int(payload.get("id") or 0),
        name=str(payload.get("name") or ""),
        enforcement=str(payload.get("enforcement") or ""),
        include_refs=tuple(str(item) for item in include) if isinstance(include, list) else (),
        exclude_refs=tuple(str(item) for item in exclude) if isinstance(exclude, list) else (),
        rule_types=tuple(rule_types),
        bypass_actors=bypass,
    )


def _ref_names(branch: str, default_branch: str) -> set[str]:
    names = {branch, f"refs/heads/{branch}"}
    if branch == default_branch:
        names.add("~DEFAULT_BRANCH")
    return names


def ruleset_covers_ref(ruleset: Ruleset, branch: str, default_branch: str) -> bool:
    names = _ref_names(branch, default_branch)
    if names.intersection(ruleset.exclude_refs):
        return False
    if "~ALL" in ruleset.include_refs:
        return True
    return bool(names.intersection(ruleset.include_refs))


def deletion_rule_blocks_auto_delete(ruleset: Ruleset) -> bool:
    if ruleset.enforcement != "active":
        return False
    if "deletion" not in ruleset.rule_types:
        return False
    if ruleset.bypass_actors is None:
        # Missing bypass_actors cannot be treated as empty: the list endpoint
        # omits them, and an omitted list would look like durable protection.
        return False
    return len(ruleset.bypass_actors) == 0


def head_survives_auto_delete(
    settings: RepoSettings,
    rulesets: Sequence[Ruleset],
    head_ref: str,
) -> bool:
    if not settings.delete_branch_on_merge:
        return True
    return any(
        ruleset_covers_ref(ruleset, head_ref, settings.default_branch)
        and deletion_rule_blocks_auto_delete(ruleset)
        for ruleset in rulesets
    )


def next_survives_auto_delete_on_merge(
    settings: RepoSettings,
    rulesets: Sequence[Ruleset],
    head_ref: str,
    next_branch: str = NEXT_BRANCH,
) -> bool:
    if head_ref != next_branch:
        return True
    return head_survives_auto_delete(settings, rulesets, next_branch)


def predict_head_branch_deleted_after_merge(
    settings: RepoSettings,
    rulesets: Sequence[Ruleset],
    head_ref: str,
) -> bool:
    """Would GitHub auto-delete `head_ref` after merging this pull request?

    Isolated fixture of GitHub's merge-deletion rule: auto-delete happens when
    `delete_branch_on_merge` is on, unless an active deletion ruleset covers
    the head and lists no bypass actors. Documented at
    https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches
    Observed on this repository's PR #2085 (`head_ref_deleted` for `next` six
    seconds after merge).
    """
    return not head_survives_auto_delete(settings, rulesets, head_ref)


def next_is_release_train_branch(
    workflow: dict[str, object], next_branch: str = NEXT_BRANCH
) -> bool:
    on = workflow.get("on")
    if not isinstance(on, dict):
        return True
    push = on.get("push")
    if not isinstance(push, dict):
        return True
    branches = push.get("branches")
    if not isinstance(branches, list):
        return True
    return next_branch in [str(item) for item in branches]


def pull_request_refs_from_event(event: dict[str, object]) -> PullRequestRefs | None:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    head = pull_request.get("head")
    base = pull_request.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return None
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    if not isinstance(head_ref, str) or not isinstance(base_ref, str):
        return None
    if not head_ref or not base_ref:
        return None
    return PullRequestRefs(head_ref=head_ref, base_ref=base_ref)


def evaluate(
    *,
    event: dict[str, object],
    settings: RepoSettings,
    rulesets: Sequence[Ruleset],
    workflow: dict[str, object],
    next_branch: str = NEXT_BRANCH,
    default_base: str = DEFAULT_BASE,
) -> None:
    refs = pull_request_refs_from_event(event)
    if refs is None:
        return
    if refs.head_ref != next_branch or refs.base_ref != default_base:
        return
    if not next_is_release_train_branch(workflow, next_branch):
        return
    if next_survives_auto_delete_on_merge(
        settings, rulesets, refs.head_ref, next_branch
    ):
        return
    raise PreserveNextError(
        "ordinary next-to-main release merge would delete protected next: "
        "delete_branch_on_merge is enabled and no active deletion ruleset "
        "covers next with an empty bypass list. Cut the release PR from a "
        "short-lived task/release-* branch, or add a next-only deletion "
        "ruleset with no bypass actors (issue #2090)."
    )


def _gh_env() -> dict[str, str]:
    """Force plain JSON. `gh api` colorizes when CLICOLOR_FORCE is set."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["GH_FORCE_TTY"] = "0"
    env.pop("CLICOLOR_FORCE", None)
    return env


def _gh_get(endpoint: str) -> object:
    result = subprocess.run(
        ["gh", "api", "-X", "GET", endpoint],
        capture_output=True,
        text=True,
        check=True,
        env=_gh_env(),
    )
    return json.loads(result.stdout)


def fetch_repo_settings(repo: str) -> RepoSettings:
    payload = _gh_get(f"repos/{repo}")
    if not isinstance(payload, dict):
        raise PreserveNextError(f"repository payload for {repo} was not an object")
    return parse_repo_settings(payload)


def fetch_rulesets(repo: str) -> list[Ruleset]:
    listing = _gh_get(f"repos/{repo}/rulesets")
    if not isinstance(listing, list):
        raise PreserveNextError(f"ruleset list for {repo} was not an array")
    rulesets: list[Ruleset] = []
    for item in listing:
        if not isinstance(item, dict) or item.get("id") is None:
            raise PreserveNextError(f"ruleset list for {repo} contained an entry without id")
        detail = _gh_get(f"repos/{repo}/rulesets/{item['id']}")
        if not isinstance(detail, dict):
            raise PreserveNextError(
                f"ruleset {item['id']} for {repo} was not an object"
            )
        rulesets.append(parse_ruleset(detail))
    return rulesets


def _push_branches_from_text(text: str) -> list[str]:
    match = _PUSH_BRANCHES.search(text)
    if match is None:
        return [NEXT_BRANCH]
    names: list[str] = []
    for item in match.group(1).split(","):
        token = item.strip()
        if token:
            names.append(token.split()[-1])
    return names


def load_release_workflow() -> dict[str, object]:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    return {"on": {"push": {"branches": _push_branches_from_text(text)}}}


def _ordinary_release_event() -> dict[str, object]:
    return {
        "pull_request": {
            "head": {"ref": NEXT_BRANCH},
            "base": {"ref": DEFAULT_BASE},
        }
    }


def _needs_live_github(
    event: dict[str, object], workflow: dict[str, object]
) -> bool:
    refs = pull_request_refs_from_event(event)
    return (
        refs is not None
        and refs.head_ref == NEXT_BRANCH
        and refs.base_ref == DEFAULT_BASE
        and next_is_release_train_branch(workflow)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", required=True, help="owner/name, e.g. curie-eng/curie"
    )
    parser.add_argument(
        "--event",
        help="GitHub event JSON path; defaults to GITHUB_EVENT_PATH, or the "
        "ordinary next-to-main path when neither is set",
    )
    args = parser.parse_args(argv)

    try:
        workflow = load_release_workflow()
        event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
        if event_path:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                raise PreserveNextError("GitHub event payload was not an object")
        else:
            event = _ordinary_release_event()
        if _needs_live_github(event, workflow):
            settings = fetch_repo_settings(args.repo)
            rulesets = fetch_rulesets(args.repo)
        else:
            settings = RepoSettings(delete_branch_on_merge=True, default_branch=DEFAULT_BASE)
            rulesets = []
        evaluate(
            event=event,
            settings=settings,
            rulesets=rulesets,
            workflow=workflow,
        )
    except PreserveNextError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, OSError) as exc:
        detail = getattr(exc, "stderr", None)
        suffix = f": {str(detail).strip()}" if detail else ""
        print(
            f"ERROR: could not retrieve repository settings for {args.repo} "
            f"-- the lookup failed with {type(exc).__name__}{suffix}. "
            "Refusing because whether next would be deleted is unknown.",
            file=sys.stderr,
        )
        return 1
    print("OK: merging this event will not auto-delete next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
