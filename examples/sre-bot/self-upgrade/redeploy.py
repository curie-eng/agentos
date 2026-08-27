#!/usr/bin/env python3
"""Redeploy this bundle when the repository it lives in has moved.

What this is for
----------------
"The bot upgrades its own version" needs three separate things, and only one of
them is a capability the bot could ever hold:

1. noticing that a newer bundle exists,
2. getting that bundle's bytes,
3. calling the platform to make it the in-force version.

The sandbox can do none of them: it holds a per-turn ``state``-scoped token, and
every ``/agents/**`` route needs the platform key. So the deploy is performed by
something OUTSIDE the sandbox -- this job -- and the bot's own authority is
unchanged. That is the same division Draft ADR-0125 draws for the control plane:
an agent may render and propose; execution sits where a sandbox cannot reach.

Why this exists next to git-flow rather than instead of it
---------------------------------------------------------
Curie already deploys a new version when a repository's branch tip moves
(``commitpoller``). It packages the repository ROOT as the bundle, so it serves a
repo whose root *is* the bundle and cannot serve a bundle living in a
subdirectory of a monorepo.

This bundle lives at ``examples/sre-bot`` inside curie, which is where it should
stay -- it ships as the worked example, and it is reviewed with the platform
changes it exercises. So the choice is not "git-flow or nothing": it is whether
the bundle moves to its own repository, or whether the subdirectory case gets
handled. This handles it, and deliberately does the same three steps git-flow
does rather than inventing a second concept.

**It is not a way around any gate.** It reads a subdirectory of a repository an
operator named and calls two documented endpoints with the platform key an
operator gave it. Nothing here decides what the bot may do; the bundle's own
``approvalPolicy`` and its connectors' ceilings are unchanged by a redeploy.

What it does NOT do
-------------------
**It does not deploy yet, and that is deliberate rather than unfinished.** The
bundle as it sits in the repository declares ``build:`` for three connectors, and
a cluster deploy is refused for an image that exists only in one machine's Docker
daemon. Uploading the repository copy verbatim would create a version whose
connectors cannot come up -- a redeploy that reports success and leaves the bot
with no tools, which is worse than not redeploying. The missing piece is digest
resolution, the same step ``curie example sre-bot install`` already performs for
tempo; it needs the write connectors published first (curie#1945).

So this build does the two steps that are correct on their own -- notice, and say
so loudly enough to act on -- and stops before the one that would lie.


It does not upgrade Curie. A new *platform* version is not something an agent can
apply to itself: the upgrade restarts the worker the agent's turn is running in,
so the turn that performed it cannot report the outcome or roll it back. Platform
upgrades stay an operator action, and this job's only relationship to one is that
it will redeploy the bundle afterwards.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
import urllib.request

# The bundle's path inside the repository. The whole reason this file exists.
BUNDLE_PREFIX = "examples/sre-bot"

# A bundle is these files and nothing else. Everything under the prefix that is
# not one of these is left out, so a stray editor file or a test fixture cannot
# ride into a deployed version.
BUNDLE_MEMBERS = (
    ".claude-plugin/plugin.json",
    "connectors.yaml",
    "deploy.yaml",
    "evals/cases.json",
)
BUNDLE_TREES = ("skills/", "manifests/")


class SelfUpgradeError(RuntimeError):
    """Something the operator has to fix, phrased for the operator."""


def _get(url: str, token: str | None, accept: str) -> bytes:
    headers = {"Accept": accept, "User-Agent": "curie-sre-bot-self-upgrade"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        # 404 on a private repository with no token is really 401, and reads as
        # "no such repo" to anyone who has not hit it before. Say the likely
        # cause rather than the status.
        if exc.code in (401, 403, 404) and not token:
            raise SelfUpgradeError(
                f"{url} returned HTTP {exc.code} with no token. A private "
                f"repository needs a read-only token in GITHUB_TOKEN; without one "
                f"this job cannot see whether a newer bundle exists."
            ) from exc
        raise SelfUpgradeError(f"{url} returned HTTP {exc.code}") from exc


def latest_commit(repo: str, branch: str, token: str | None) -> str:
    """The sha at the tip of ``branch``, which is what "newer" means here."""

    body = _get(
        f"https://api.github.com/repos/{repo}/commits/{branch}",
        token,
        "application/vnd.github+json",
    )
    sha = json.loads(body).get("sha")
    if not isinstance(sha, str) or not sha:
        raise SelfUpgradeError(f"{repo}@{branch} returned no commit sha")
    return sha


def deployed_commit(api_url: str, api_key: str, agent: str) -> tuple[str, str | None]:
    """``(agent_id, commit_sha)`` for the agent's newest version.

    ``None`` for the sha when the newest version records none -- a version
    deployed by hand from a working copy has no commit, and that must read as
    "unknown", never as "up to date".
    """

    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/agents", headers={"X-API-Key": api_key}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        agents = json.load(response)
    match = next((a for a in agents if a.get("name") == agent), None)
    if match is None:
        raise SelfUpgradeError(
            f"no agent named {agent!r} on {api_url}; this job redeploys an agent "
            f"that already exists rather than creating one"
        )
    agent_id = match["id"]
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/agents/{agent_id}/versions",
        headers={"X-API-Key": api_key},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        versions = json.load(response)
    if not versions:
        return agent_id, None
    newest = sorted(versions, key=lambda v: v["created_at"])[-1]
    return agent_id, newest.get("commit_sha")


def _wanted(name: str) -> bool:
    if name in BUNDLE_MEMBERS:
        return True
    return any(name.startswith(tree) for tree in BUNDLE_TREES)


def bundle_from_repo_tarball(archive: bytes, prefix: str = BUNDLE_PREFIX) -> bytes:
    """Re-tar the bundle subdirectory out of a repository tarball.

    GitHub's tarball wraps everything in one top-level directory whose name
    carries the sha, so the prefix cannot be hardcoded and is discovered from the
    first member instead.

    Members are filtered to the bundle's own files. Directory entries, symlinks
    and anything outside the prefix are dropped -- a symlink inside an archive is
    how a tar extraction escapes its directory, and this bundle has no use for
    one.
    """

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        members = source.getmembers()
        if not members:
            raise SelfUpgradeError("repository tarball is empty")
        root = members[0].name.split("/", 1)[0]
        want = f"{root}/{prefix}/"
        out = io.BytesIO()
        found = 0
        with tarfile.open(fileobj=out, mode="w:gz") as bundle:
            for member in members:
                if not member.isfile() or not member.name.startswith(want):
                    continue
                relative = member.name[len(want) :]
                if not _wanted(relative):
                    continue
                extracted = source.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = 0o644
                bundle.addfile(info, io.BytesIO(data))
                found += 1
        if found == 0:
            raise SelfUpgradeError(
                f"no bundle files under {prefix!r} in the repository tarball; "
                f"check the path and the branch"
            )
    return out.getvalue()


def main() -> int:
    repo = os.environ.get("CURIE_SELF_UPGRADE_REPO", "curie-eng/curie")
    branch = os.environ.get("CURIE_SELF_UPGRADE_BRANCH", "main")
    agent = os.environ.get("CURIE_SELF_UPGRADE_AGENT", "sre-bot")
    api_url = os.environ.get("CURIE_API_URL")
    api_key = os.environ.get("CURIE_API_KEY")
    token = os.environ.get("GITHUB_TOKEN") or None
    dry_run = os.environ.get("CURIE_SELF_UPGRADE_DRY_RUN", "").lower() in ("1", "true")

    if not api_url or not api_key:
        print("CURIE_API_URL and CURIE_API_KEY are required", flush=True)
        return 2

    try:
        tip = latest_commit(repo, branch, token)
        agent_id, current = deployed_commit(api_url, api_key, agent)
    except SelfUpgradeError as exc:
        # Exit non-zero: a job that cannot tell whether it is behind must not
        # report success. A silent "nothing to do" is the failure this whole file
        # is a reaction to.
        print(f"cannot determine whether {agent} is behind: {exc}", flush=True)
        return 1

    print(f"{agent}: deployed={current or 'unknown'} tip={tip[:12]}", flush=True)
    if current == tip:
        print("already on the repository tip; nothing to do", flush=True)
        return 0
    if dry_run:
        print("dry run: would deploy the bundle at the tip", flush=True)
        return 0

    print(
        "a newer bundle exists. This job does not deploy it yet: the bundle in the "
        "repository declares `build:` for three connectors, and a cluster deploy is "
        "refused for an image that exists only in a local daemon. Deploying from "
        "here needs the published digests first (curie#1945), the way the installer "
        "already resolves tempo's.",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
