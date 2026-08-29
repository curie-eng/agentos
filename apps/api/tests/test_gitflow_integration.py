"""Git-flow end to end against real Postgres + RustFS with local bare repos.

No github.com and no network: the "remote" is a local bare repository and the
webhook payloads are HMAC-signed exactly as GitHub signs them. This exercises
the real deploy/promote path (git archive -> validate -> store -> deploy row).

The bare repositories live under a per-test `file://` clone base laid out as
`<base>/<owner>/<name>.git`, and `GITHUB_CLONE_BASE` points at that base, so the
URL git is handed is the derived origin the production code computes -- the same
way a GitHub Enterprise operator points the setting at their own host. Nothing
here is a test-only code branch.
"""

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from curie_api import bundles, crud
from curie_api.config import get_settings
from curie_api.deps import get_eval_queue
from curie_test_support.scaffold import scaffolded_deploy_yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

SECRET = get_settings().github_webhook_secret
REPO = "octo/demo-agent"

VALID_FILES = {
    ".claude-plugin/plugin.json": '{"name": "demo-plugin", "version": "0.1.0"}',
    "skills/alpha/SKILL.md": "---\nname: alpha\ndescription: does alpha\n---\n",
    "skills/beta/SKILL.md": "---\nname: beta\ndescription: does beta\n---\n",
}


@pytest.fixture
def trusted_clone_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point `GITHUB_CLONE_BASE` at a per-test `file://` base and yield it.

    `routers/github.py` calls `get_settings()` per request rather than through a
    cached dependency, so the app does not need rebuilding. The clear-AFTER-undo
    ordering at teardown is load-bearing: clearing first would let the still-set
    env value be re-cached and leak into a later test.
    """

    base = tmp_path / "remotes"
    base.mkdir()
    monkeypatch.setenv("GITHUB_CLONE_BASE", f"file://{base}")
    get_settings.cache_clear()
    yield base
    monkeypatch.undo()
    get_settings.cache_clear()


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _build_bare_repo(base_dir: Path, repo_full_name: str, files: dict[str, str]) -> tuple[str, str]:
    """Create a bare repo at `<base_dir>/<repo_full_name>.git`. Returns (url, sha).

    The returned URL is byte-identical to the origin the API derives from
    `GITHUB_CLONE_BASE` plus the agent's stored `repo_full_name`, so these tests
    exercise the derivation instead of bypassing it.
    """

    work = base_dir / "_work" / repo_full_name
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "dev", cwd=work)
    for rel, content in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    bare = base_dir / f"{repo_full_name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--quiet", "--bare", str(work), str(bare))
    _git("update-ref", "refs/heads/main", sha, cwd=bare)
    return f"file://{bare}", sha


def _build_bare_repo_with_pull_only_commit(
    base_dir: Path, repo_full_name: str, files: dict[str, str]
) -> tuple[str, str]:
    """Add a valid commit only at ``refs/pull/1/head`` in the bare repo."""

    clone_url, _dev_sha = _build_bare_repo(base_dir, repo_full_name, files)
    work = base_dir / "_work" / repo_full_name
    marker = work / "PULL_ONLY.txt"
    marker.write_text("reachable only from the pull ref\n")
    _git("add", marker.name, cwd=work)
    _git("commit", "-q", "-m", "pull ref commit", cwd=work)
    pull_sha = _git("rev-parse", "HEAD", cwd=work)

    bare = base_dir / f"{repo_full_name}.git"
    _git(
        "push",
        "--quiet",
        str(bare),
        f"{pull_sha}:refs/pull/1/head",
        cwd=work,
    )
    return clone_url, pull_sha


def _post(client: Any, event: str, payload: dict[str, Any], secret: str = SECRET) -> Any:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def _push_payload(ref: str, sha: str, clone_url: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "after": sha,
        "repository": {"full_name": REPO, "clone_url": clone_url},
    }


def _insert_partial_version(agent_id: str, sha: str) -> None:
    """Insert a version row with commit_sha set but no bundle stored.

    Mimics the residue of a prior push that committed the row and then failed
    before the bundle was stored.
    """

    async def _run() -> None:
        engine = create_async_engine(get_settings().database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            await crud.create_version_row(
                session,
                uuid.UUID(agent_id),
                version_label=sha[:12],
                created_by="git-flow",
                commit_sha=sha,
            )
        await engine.dispose()

    asyncio.run(_run())


def _set_legacy_repo_full_name(agent_id: str, repo_full_name: str) -> None:
    async def _run() -> None:
        engine = create_async_engine(get_settings().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE curie.agents SET repo_full_name = :repo_full_name "
                    "WHERE id = :agent_id"
                ),
                {"repo_full_name": repo_full_name, "agent_id": uuid.UUID(agent_id)},
            )
        await engine.dispose()

    asyncio.run(_run())


def _register_agent(client: Any, headers: dict[str, str]) -> str:
    agent = client.post(
        "/agents",
        json={
            "name": "gitflow-agent",
            "channel": {"kind": "slack", "address": "C000000G01"},
            "repo_full_name": REPO,
        },
        headers=headers,
    ).json()
    return str(agent["id"])


class _ExtractSpy:
    """Counts `bundles.extract_and_validate` calls while still doing the real work.

    Patched onto the MODULE object (`monkeypatch.setattr(bundles, ...)`), never
    onto an import alias: both production call sites -- `gitflow._read_targets`
    and `deploy.validate_archive` -- resolve the name as
    `bundles.extract_and_validate` at call time, so this one patch counts both.

    It delegates to the captured original rather than returning canned values,
    so the delivery under test stays a real delivery. A stub would make "zero
    extractions" true by making the push meaningless, which is the failure mode
    a call-count assertion is most prone to.
    """

    def __init__(self) -> None:
        self.calls = 0
        # Captured BEFORE the patch is installed, so the spy wraps the real
        # function and never itself.
        self._real = bundles.extract_and_validate

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real(*args, **kwargs)


class _RecordingEvalQueue:
    """Stands in for the Valkey eval producer and remembers what was enqueued.

    Installed through `app.dependency_overrides[get_eval_queue]`, the same
    public seam `test_webhook_body_bound.py` uses, rather than by reaching into
    the queue's internals or reading the stream back out of Valkey.
    """

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> str:
        self.jobs.append(job)
        return f"0-{len(self.jobs)}"


def _delete_bare_repo(base_dir: Path, repo_full_name: str = REPO) -> None:
    """Make the remote unreachable, the way a dead PAT or a deleted repo does.

    #1110 records the operational expectation this rests on: `github_token` is a
    static PAT that "dies when they leave". A dead credential does not stop
    GitHub delivering webhooks -- it stops only the clone -- so "the push
    arrives but the remote cannot be read" is a state the platform is expected
    to meet, not a contrived one. Removing the bare repository under the derived
    origin reproduces exactly that split.

    The `_work` tree is deliberately left in place: the derived origin is
    `<base>/<owner>/<name>.git` and nothing else, so nothing can resolve through
    it. The assertion below is what keeps this helper honest -- a silent no-op
    here would turn every test that calls it into a test of nothing.
    """

    bare = base_dir / f"{repo_full_name}.git"
    shutil.rmtree(bare)
    assert not bare.exists(), "the remote must really be gone, or these tests prove nothing"


def _tighten_uncompressed_cap(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Shrink `bundle_max_uncompressed_bytes` for the rest of the test.

    Same two moves as `trusted_clone_base`: set the env, then clear the settings
    cache so the next per-request `get_settings()` picks the new value up. It
    needs no teardown of its own precisely because it writes through the SAME
    `monkeypatch` instance that fixture holds, so that fixture's clear-AFTER-undo
    ordering covers this variable too -- undo first, THEN clear, or the still-set
    value is simply re-cached and leaks into the next test.
    """

    monkeypatch.setenv("BUNDLE_MAX_UNCOMPRESSED_BYTES", value)
    get_settings.cache_clear()


def test_dev_push_deploys_dev_bot(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    resp = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["environment"] == "dev"
    assert body["commit_sha"] == sha

    # The version was built from the commit and its bundle is stored + fetchable.
    version = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()[0]
    assert version["commit_sha"] == sha
    assert version["bundle_ref"] is not None
    bundle = client.get(f"/agents/{agent_id}/versions/{version['id']}/bundle", headers=auth_headers)
    assert bundle.status_code == 200
    assert len(bundle.content) > 0

    # The deployment routes to the dev bot.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert len(deployments) == 1
    assert deployments[0]["environment"] == "dev"


def test_signed_push_rejects_a_commit_unreachable_from_its_branch(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    clone_url, pull_sha = _build_bare_repo_with_pull_only_commit(
        trusted_clone_base, REPO, VALID_FILES
    )

    response = _post(
        client,
        "push",
        _push_payload("refs/heads/dev", pull_sha, clone_url),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected", body
    assert {error["code"] for error in body["errors"]} == {
        "git.commit_not_on_branch"
    }
    assert client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json() == []
    assert (
        client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers).json()
        == []
    )


def test_signed_push_rejects_an_invalid_legacy_repository_binding(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    invalid_repo = "octo/.."
    _set_legacy_repo_full_name(agent_id, invalid_repo)
    payload = _push_payload(
        "refs/heads/dev",
        "a" * 40,
        f"file://{trusted_clone_base}/{REPO}.git",
    )
    payload["repository"]["full_name"] = invalid_repo

    response = _post(client, "push", payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected", body
    assert {error["code"] for error in body["errors"]} == {
        "git.invalid_repository"
    }
    assert client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json() == []
    assert (
        client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers).json()
        == []
    )


def test_patched_repo_binding_routes_a_push(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """A binding applied by PATCH must actually route that repository's pushes.

    Before migration 0018, create-time was the only place `repo_full_name`
    could ever be set, so an agent bound after creation had no way to become
    reachable by git-flow. This drives the push through the single-agent
    fallback in `resolve_target_agent` (no `deploy.yaml` in the pushed files,
    exactly one agent bound to the repository), which is the path an agent
    bound entirely by PATCH depends on.

    Asserting that `AgentUpdate` has the field proves none of that. Only a real
    signed push landing a deployment on the patched agent does.
    """

    created = client.post(
        "/agents",
        json={"name": "patched-agent", "channel": {"kind": "slack", "address": "C000000G02"}},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    assert created.json()["repo_full_name"] is None, "created deliberately unbound"

    patched = client.patch(
        f"/agents/{agent_id}",
        json={"repo_full_name": REPO},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["repo_full_name"] == REPO

    # Read back through a separate request, so the assertion cannot be satisfied
    # by the PATCH response echoing its own input.
    fetched = client.get(f"/agents/{agent_id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["repo_full_name"] == REPO

    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    resp = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deployed", body
    assert body["environment"] == "dev"
    assert body["commit_sha"] == sha

    # The push reached THIS agent, which is the whole point of the binding: a
    # push succeeding somewhere else would prove nothing.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert len(deployments) == 1, deployments
    assert deployments[0]["environment"] == "dev"


def test_patch_omitting_repo_full_name_leaves_the_binding_intact(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    """A PATCH that omits repo_full_name must leave an existing binding alone.

    This is the guarantee a channel-only redeploy PATCH depends on: the CLI
    sends slack_channel by itself whenever --repo was not passed, and that
    must never wipe the repository the agent is already bound to.
    """

    created = client.post(
        "/agents",
        json={
            "name": "omit-repo-agent",
            "channel": {"kind": "slack", "address": "C0EXAMPLE3"},
            "repo_full_name": REPO,
        },
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    agent_id = str(created.json()["id"])
    assert created.json()["repo_full_name"] == REPO, "bound at creation"

    patched = client.patch(
        f"/agents/{agent_id}",
        json={"channel": {"kind": "slack", "address": "C0EXAMPLE4"}},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text

    # Read back through a separate request, so the assertion cannot be
    # satisfied by the PATCH response echoing stale input.
    fetched = client.get(f"/agents/{agent_id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["repo_full_name"] == REPO, (
        "omitting repo_full_name from the PATCH must leave the binding unchanged"
    )
    assert fetched.json()["channel"] == {"kind": "slack", "address": "C0EXAMPLE4"}, (
        "the channel PATCH must still take effect"
    )


def test_main_push_promotes_and_reuses_the_built_version(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    prod = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()

    assert prod["status"] == "promoted"
    assert prod["environment"] == "prod"
    # Promote reuses the already-built bundle rather than rebuilding.
    assert prod["version_id"] == dev["version_id"]

    envs = {
        d["environment"]
        for d in client.get(
            "/deployments", params={"agent_id": agent_id}, headers=auth_headers
        ).json()
    }
    assert envs == {"dev", "prod"}


def test_partial_version_is_rebuilt_not_reused(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    _insert_partial_version(agent_id, sha)

    resp = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    assert resp.status_code == 200
    assert resp.json()["status"] == "deployed"

    versions = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()
    # The partial row was repaired in place (no duplicate) and now has a bundle.
    assert len(versions) == 1
    assert versions[0]["commit_sha"] == sha
    assert versions[0]["bundle_ref"] is not None


def test_invalid_signature_is_401(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    body = json.dumps(_push_payload("refs/heads/dev", "a" * 40, "file:///x")).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401


def test_ping_event_pongs(client: Any, auth_headers: dict[str, str], clean_db: None) -> None:
    resp = _post(client, "ping", {"zen": "hi"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pong"


def test_unknown_repo_is_ignored(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # No agent registered for REPO.
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    resp = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_non_deploy_branch_is_ignored(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    resp = _post(client, "push", _push_payload("refs/heads/feature-x", sha, clone_url))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_malformed_bundle_push_is_rejected(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    files = dict(VALID_FILES)
    files["skills/beta/SKILL.md"] = "# no frontmatter\n"
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, files)

    resp = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    codes = {e["code"] for e in body["errors"]}
    assert "skill.frontmatter_missing" in codes

    # Nothing was deployed for a rejected push.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert deployments == []


def test_signed_push_with_a_foreign_clone_url_is_rejected(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # The ticket's attack driven through the real consumer path: a correctly
    # HMAC-signed push naming the registered repository, with an attacker's
    # clone URL. A real bare repo exists at the trusted path and the same push
    # deploys fine when the URL matches (test_dev_push_deploys_dev_bot), so the
    # rejection here is the origin pin and nothing else.
    agent_id = _register_agent(client, auth_headers)
    _url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    resp = _post(
        client,
        "push",
        _push_payload("refs/heads/dev", sha, f"https://evil.example/{REPO}.git"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    codes = {e["code"] for e in body["errors"]}
    assert "git.origin_mismatch" in codes

    # Nothing was built and nothing was deployed.
    assert client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json() == []
    assert (
        client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers).json() == []
    )


def test_push_using_the_repository_url_fallback_still_deploys(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # `repository.url` is the rarely-executed sibling of `repository.clone_url`
    # at gitflow.py:189. GitHub's push payload carries `clone_url` WITH the .git
    # suffix and `url` WITHOUT it, so this arms the guard via the secondary path
    # only and proves it agrees with the primary path (AGENTS.md parity-seam
    # rule).
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    resp = _post(
        client,
        "push",
        {
            "ref": "refs/heads/dev",
            "after": sha,
            "repository": {
                "full_name": REPO,
                "url": clone_url.removesuffix(".git"),
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deployed"

    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert len(deployments) == 1


def test_signed_push_with_a_foreign_url_fallback_is_rejected(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # The fallback must not be an unguarded back door: the same comparison
    # applies whichever payload field supplied the URL.
    agent_id = _register_agent(client, auth_headers)
    _url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    resp = _post(
        client,
        "push",
        {
            "ref": "refs/heads/dev",
            "after": sha,
            "repository": {
                "full_name": REPO,
                "url": f"https://evil.example/{REPO}",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    assert "git.origin_mismatch" in {e["code"] for e in body["errors"]}

    assert client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json() == []
    assert (
        client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers).json() == []
    )


# --------------------------------------------------------------------------- #
# A promote reuses the stored bundle instead of re-cloning (#1211)
# --------------------------------------------------------------------------- #
# #1194 hoisted `clone_and_archive` + `deploy.validate_archive` above the
# `if bundle_built:` guard, because deploy.yaml -- which decides the target
# agent -- lives inside the bundle. The hoist is right; what it cost is that a
# prod promote now pays a full mirror clone plus two extractions for bytes it
# then discards, and FAILS outright when the remote is unreachable. These tests
# pin the promote's reuse of the already-stored, already-validated object, and
# they pin the guards that must survive the reordering.
#
# The measure is calls to `bundles.extract_and_validate` -- the composite
# (detect + extract + validate_bundle) whose second and third parts are the
# redundant work on this path, and the one the issue's own instrumentation
# counted. A promote may still bounded-extract the stored bytes to read one
# YAML file; what it must not do is re-run whole-bundle validation on an
# artifact that passed it at store time under an immutable, write-once key.


def test_prod_promote_reuses_the_stored_bundle_with_the_remote_deleted(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1211's acceptance criterion, driven through the real webhook.

    Dev builds and stores the bundle; the remote then disappears; prod promotes
    the same sha anyway, reusing the same version and touching no network.

    Two mutations this must catch, and they are caught by DIFFERENT assertions:

    * Reverting the branch so the clone runs unconditionally reddens step 5 --
      the promote comes back `rejected` with `git.archive_failed`, because the
      bare repository is gone.
    * Keeping the reuse but feeding the stored bytes to `_read_targets` (the
      issue's literal Fix wording) instead of `_read_stored_targets` reddens the
      final assertion with `spy.calls == 1`.

    Neither mutation is caught by the other's assertion, which is why both are
    here.
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev
    assert dev["version_id"] is not None

    _delete_bare_repo(trusted_clone_base)

    # Installed only NOW. Counting the dev delivery too would leave the test
    # unable to say WHICH delivery did the work, and the dev push must run the
    # real validation or there is nothing trustworthy to promote.
    spy = _ExtractSpy()
    monkeypatch.setattr(bundles, "extract_and_validate", spy)

    prod = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()

    assert prod["status"] == "promoted", prod
    assert prod["environment"] == "prod"
    assert prod["version_id"] == dev["version_id"], (
        "prod must promote the artifact dev validated, not a rebuild of it"
    )
    assert spy.calls == 0, (
        f"a promote of an already-stored bundle must re-validate nothing; got {spy.calls}"
    )

    # "promoted" is a string; a Deployment row is the product. Read it back
    # through the API rather than trusting the response body's own report.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    prod_rows = [d for d in deployments if d["environment"] == "prod"]
    assert len(prod_rows) == 1, deployments
    assert prod_rows[0]["commit_sha"] == sha
    assert prod_rows[0]["version_id"] == dev["version_id"]


def test_a_redelivered_dev_push_reuses_the_version_without_rebuilding(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """A redelivered dev push is idempotent: one version, one eval job.

    GitHub redelivers a webhook whenever the first attempt looks unhealthy, and
    an operator can redeliver by hand from the deliveries UI. The version is
    already built and stored, so the second delivery must reuse that row rather
    than build a second one or fan out a second eval.

    What it must NOT claim is that the dev lane skips the clone. The dev lane
    deliberately still clones on every delivery, because the clone is where
    `git merge-base --is-ancestor` (#1139) runs -- see
    `test_a_dev_push_for_a_sha_no_longer_on_the_branch_is_still_rejected`.
    #1211's stored-bundle fast path is therefore prod-only, and the
    dev-redelivery row of that issue's table is left unfixed on purpose: a dev
    redelivery still pays for its clone, and that is the price of keeping the
    ancestry check on the lane an attacker can reach with a signed replay. So
    the remote stays up here, and what is pinned is idempotence, not
    offline-ness.

    The eval queue is recorded across BOTH deliveries on purpose: asserting only
    "zero on the second" would also pass if the fan-out had stopped working
    entirely. One job after the first delivery and still one after the second is
    what pins `environment is dev and bundle_built`'s dedupe (6c3698e1).
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    eval_queue = _RecordingEvalQueue()
    client.app.dependency_overrides[get_eval_queue] = lambda: eval_queue
    try:
        first = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
        assert first["status"] == "deployed", first
        assert len(eval_queue.jobs) == 1, "the first dev delivery fans out one eval job"

        second = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    finally:
        client.app.dependency_overrides.pop(get_eval_queue, None)

    assert second["status"] == "deployed", second
    assert second["version_id"] == first["version_id"], (
        "a redelivery must reuse the version dev already built, not build a second one"
    )
    assert len(eval_queue.jobs) == 1, (
        "a redelivered push must not enqueue a second eval job for the same version"
    )

    versions = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()
    assert len(versions) == 1, versions
    assert versions[0]["id"] == first["version_id"]

    # Each delivery records its own Deployment row -- that is the audit trail --
    # but both point at the one version, which is the property that matters.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert [d["environment"] for d in deployments] == ["dev", "dev"], deployments
    assert {d["version_id"] for d in deployments} == {first["version_id"]}


def test_a_dev_push_for_a_sha_no_longer_on_the_branch_is_still_rejected(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """#1211's reuse must never reach the dev lane, or #1139 stops guarding it.

    The exploit this prevents: a holder of the webhook signing secret replays a
    dev push naming a sha that was deployed to dev once and has since been
    force-pushed off the branch. A bundle for that sha is already stored, so a
    reuse lookup that ran for dev would hand those bytes straight back to the
    dev bot without ever asking git whether the commit is still on the branch --
    a signed rollback of the dev agent onto abandoned code. Keeping the
    stored-bundle fast path prod-only is what keeps `git merge-base
    --is-ancestor` on every dev delivery.

    Mutation this catches: widening the reuse lookup back to dev flips this
    red-to-green in the wrong direction -- the push would come back `deployed`,
    served from the stored bundle, with the clone and its ancestry check never
    run. The test fails exactly when the bypass is widened.
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    # A real dev deploy first, so a stored bundle for this sha exists. Without
    # one the reuse path could not fire even if it were wired to dev, and the
    # rejection below would prove nothing about the bypass.
    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev

    # Roll the branch off that sha the way a force-push does. The replacement is
    # an ORPHAN commit: a descendant would leave `sha` an ancestor of the branch
    # and the ancestry check would pass. `sha` itself must stay a live object --
    # `refs/heads/main` still holds it -- so `merge-base` answers "not an
    # ancestor" instead of failing on a missing object, which is a different
    # rejection and would not prove the check ran.
    work = trusted_clone_base / "_work" / REPO
    bare = trusted_clone_base / f"{REPO}.git"
    _git("checkout", "-q", "--orphan", "rewritten", cwd=work)
    (work / "REWRITTEN.txt").write_text("history rewritten over the deployed sha\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "force-push over dev", cwd=work)
    rewritten_sha = _git("rev-parse", "HEAD", cwd=work)
    # Push under a non-branch ref first so the object exists in the bare repo,
    # then move the branch itself: `refs/heads/dev` is a non-fast-forward here,
    # which is exactly what a force-push is.
    _git("push", "--quiet", str(bare), f"{rewritten_sha}:refs/rewritten/head", cwd=work)
    _git("update-ref", "refs/heads/dev", rewritten_sha, cwd=bare)
    assert _git("rev-parse", "refs/heads/dev", cwd=bare) == rewritten_sha, (
        "the branch must really have moved, or this test proves nothing"
    )

    body = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()

    assert body["status"] == "rejected", body
    assert {error["code"] for error in body["errors"]} == {"git.commit_not_on_branch"}, body

    # The first dev deployment stands; the replay recorded nothing of its own.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert [d["environment"] for d in deployments] == ["dev"], deployments


@pytest.mark.parametrize(
    "repository",
    [
        {"full_name": REPO, "clone_url": f"https://evil.example/{REPO}.git"},
        # GitHub sets `clone_url` with the .git suffix and `url` without it; the
        # fallback field must not be an unguarded back door on this path either
        # (the parity-seam precedent is the existing clone_url/url pair above).
        {"full_name": REPO, "url": f"https://evil.example/{REPO}"},
    ],
    ids=["clone_url", "url_fallback"],
)
def test_a_foreign_clone_url_is_rejected_even_when_the_bundle_is_already_stored(
    repository: dict[str, str],
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """#1122's origin pin must still fire on the path that no longer clones.

    A dev push has already stored a bundle for this sha, so the promote takes
    the reuse path. If the origin check lived inside `clone_and_archive`, the
    branch that skips the clone would skip the check with it, and a correctly
    signed payload naming somebody else's repository would be promoted. Hoisting
    the check out is what keeps this rejection identical on both paths.

    Mutation this must catch: moving `verify_push_origin` into the clone-only
    branch turns this green-to-red -- the push would come back `promoted`.

    Note for the TDD run: unlike the reuse tests above, this one is GREEN before
    the fix as well (today the clone runs unconditionally and its own pre-flight
    rejects). That is the point of it. It is a guard against the fix dropping a
    control, so it cannot be red first without asserting something false.
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev

    body = _post(
        client,
        "push",
        {"ref": "refs/heads/main", "after": sha, "repository": repository},
    ).json()

    assert body["status"] == "rejected", body
    assert "git.origin_mismatch" in {e["code"] for e in body["errors"]}

    # The dev deployment is untouched and nothing was promoted.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert [d["environment"] for d in deployments] == ["dev"], deployments


def test_a_version_without_a_stored_bundle_does_not_take_the_reuse_path(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with `bundle_ref` NULL is residue, not a promotable artifact.

    d3a0f4b8 introduced `bundle_built = version is None or version.bundle_ref is
    None` for exactly this: a row that committed and then failed before its
    bundle was stored must be REBUILT, never deployed. The reuse lookup is the
    one place that could quietly undo it.

    Mutation this must catch: writing the lookup as "any version for this sha"
    -- dropping the `bundle_ref` truthiness test, or using `is not None` so an
    empty-string ref sails into `store.get("")` -- turns this red, because the
    delivery would reuse nothing and extract nothing.

    Green before the fix as well, deliberately: it pins behaviour the fix must
    preserve rather than behaviour the fix adds.
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    _insert_partial_version(agent_id, sha)

    # The remote is deliberately still present: this half asserts the delivery
    # genuinely goes and rebuilds.
    spy = _ExtractSpy()
    monkeypatch.setattr(bundles, "extract_and_validate", spy)

    body = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert body["status"] == "deployed", body
    assert spy.calls >= 1, "a bundleless row must send the delivery back to the remote"

    versions = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()
    assert len(versions) == 1, "the partial row is repaired in place, not duplicated"
    assert versions[0]["commit_sha"] == sha
    assert versions[0]["bundle_ref"] is not None


def test_a_bundleless_version_is_not_promotable_when_the_remote_is_gone(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """The negative half: a row alone must not buy the offline path.

    The reuse path is earned by a STORED bundle, not by the existence of a
    version row for the sha. With nothing stored there is nothing to deploy, so
    the only correct answer is the rejection the clone failure produces -- a
    bundleless row must never become promotable just because a row exists.
    """

    agent_id = _register_agent(client, auth_headers)
    _clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    _insert_partial_version(agent_id, sha)
    _delete_bare_repo(trusted_clone_base)

    body = _post(
        client,
        "push",
        _push_payload("refs/heads/main", sha, f"file://{trusted_clone_base}/{REPO}.git"),
    ).json()

    assert body["status"] == "rejected", body
    assert {e["code"] for e in body["errors"]} == {"git.archive_failed"}, body
    assert (
        client.get("/deployments", params={"agent_id": agent_id}, headers=auth_headers).json()
        == []
    )


def test_a_stored_bundle_over_the_current_caps_is_still_rejected_as_too_large(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A performance reorder must not silently downgrade an error contract.

    ADR-0059 decision 3 commits to a specific operator-facing outcome: a bundle
    stored before the current caps existed (or under looser ones) is refused at
    deploy time as `bundle.too_large`, with a message saying it must be rebuilt
    and re-uploaded. `deploy.revalidate_stored_bundle` is what produces that.

    Reading deploy.yaml off the stored bytes puts a bounded extract IN FRONT of
    that revalidation, so a bundle over the caps now blows up there first -- and
    a bounded extract's natural failure is `bundle.unsupported`, a different,
    vaguer code that tells the operator nothing about size. The reordering must
    map that one failure back, because on already-stored bytes the caps are the
    only reachable cause: unsafe entries were refused before the object could be
    stored, and the storage key is immutable and write-once.

    This is the only test on the git-flow path pinning that code, so asserting
    the exact set rather than `in` is the point -- `bundle.unsupported` passing
    for `bundle.too_large` is precisely the regression.
    """

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    # Stored under the default caps, exactly as a legacy bundle was.
    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev

    # Now the operator tightens the caps under it.
    _tighten_uncompressed_cap(monkeypatch, "1")

    body = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()

    assert body["status"] == "rejected", body
    assert {e["code"] for e in body["errors"]} == {"bundle.too_large"}, body

    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert [d["environment"] for d in deployments] == ["dev"], deployments


# --------------------------------------------------------------------------- #
# One repository, two agents (ADR-0091, #1070)
# --------------------------------------------------------------------------- #
DEPLOY_YAML = """
targets:
  dev:
    agent: two-agent-dev
    env: dev
    slack_channel: C000000D01
  prod:
    agent: two-agent-prod
    env: prod
    slack_channel: C000000E01
"""

TWO_TARGET_FILES = {**VALID_FILES, "deploy.yaml": DEPLOY_YAML}


def _register(client: Any, headers: dict[str, str], name: str, channel: str) -> str:
    resp = client.post(
        "/agents",
        json={
            "name": name,
            "channel": {"kind": "slack", "address": channel},
            "repo_full_name": REPO,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_one_repo_binds_two_agents(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # Before 0018 the second create was a 409 from the unique index, which is
    # what forced the first adopting agent repo to make its second agent out of
    # band.
    _register(client, auth_headers, "two-agent-dev", "C000000D01")
    _register(client, auth_headers, "two-agent-prod", "C000000E01")


def test_dev_push_and_main_push_reach_different_agents(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    """The thing #1070 is for: dev -> the dev bot, main -> the prod bot."""

    dev_id = _register(client, auth_headers, "two-agent-dev", "C000000D01")
    prod_id = _register(client, auth_headers, "two-agent-prod", "C000000E01")
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, TWO_TARGET_FILES)

    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev
    assert dev["agent_id"] == dev_id, "a dev push must reach the dev agent"

    prod = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()
    assert prod["status"] == "promoted", prod
    assert prod["agent_id"] == prod_id, "a main push must reach the prod agent"

    # Each agent holds exactly its own deployment, in its own environment.
    for agent_id, expected in ((dev_id, "dev"), (prod_id, "prod")):
        deployments = client.get(
            "/deployments", params={"agent_id": agent_id}, headers=auth_headers
        ).json()
        assert [d["environment"] for d in deployments] == [expected]


def test_prod_promotes_the_exact_artifact_dev_validated(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # Bundle-once, bind-many. The two agents get their own Version ROWS (the
    # worker's binding join requires a version to belong to its agent), but both
    # rows point at the SAME stored object -- so prod promotes not merely
    # identical bytes but the same artifact, by construction rather than by
    # discipline.
    dev_id = _register(client, auth_headers, "two-agent-dev", "C000000D01")
    prod_id = _register(client, auth_headers, "two-agent-prod", "C000000E01")
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, TWO_TARGET_FILES)

    _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url))
    _post(client, "push", _push_payload("refs/heads/main", sha, clone_url))

    def only_version(agent_id: str) -> dict[str, Any]:
        versions = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()
        assert len(versions) == 1, versions
        return dict(versions[0])

    dev_version, prod_version = only_version(dev_id), only_version(prod_id)
    assert dev_version["id"] != prod_version["id"], "each agent owns its own version row"
    assert dev_version["bundle_ref"] == prod_version["bundle_ref"], (
        "prod must promote the artifact dev validated, not a re-upload of it"
    )
    assert dev_version["commit_sha"] == prod_version["commit_sha"] == sha


def test_prod_promote_to_a_sibling_agent_skips_the_clone(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two-agent shape (#1211 on top of ADR-0091) also needs no remote.

    This is the harder half of the reuse. On the legacy single-agent shape the
    promote finds the agent's OWN version and `bundle_built` is False. Here the
    main push resolves to a DIFFERENT agent, which has no version for this sha,
    so `bundle_built` is True and the delivery takes the bundle-once/bind-many
    branch: it creates a row for the prod agent and attaches the dev agent's
    stored object to it rather than uploading the bytes again.

    Both halves therefore have to work offline -- the deploy.yaml read that
    decides the target, and the sibling lookup that supplies the object -- and
    `store_bundle` must never be reached, because there are no fresh bytes to
    store. `test_prod_promotes_the_exact_artifact_dev_validated` asserts the
    shared `bundle_ref` with the remote up; this asserts it survives with the
    remote gone, which is the assertion the clone could previously satisfy by
    accident.
    """

    dev_id = _register(client, auth_headers, "two-agent-dev", "C000000D01")
    prod_id = _register(client, auth_headers, "two-agent-prod", "C000000E01")
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, TWO_TARGET_FILES)

    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev
    assert dev["agent_id"] == dev_id

    _delete_bare_repo(trusted_clone_base)

    spy = _ExtractSpy()
    monkeypatch.setattr(bundles, "extract_and_validate", spy)

    prod = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()

    assert prod["status"] == "promoted", prod
    assert prod["agent_id"] == prod_id, "deploy.yaml still routes main to the prod agent"
    assert spy.calls == 0, f"the sibling path must re-validate nothing; got {spy.calls}"

    def only_version(agent_id: str) -> dict[str, Any]:
        versions = client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json()
        assert len(versions) == 1, versions
        return dict(versions[0])

    dev_version, prod_version = only_version(dev_id), only_version(prod_id)
    assert dev_version["id"] != prod_version["id"], "each agent still owns its own row"
    assert dev_version["bundle_ref"] == prod_version["bundle_ref"], (
        "with no remote to clone from, the prod row must point at the object dev stored"
    )
    assert prod_version["bundle_ref"] is not None

    deployments = client.get(
        "/deployments", params={"agent_id": prod_id}, headers=auth_headers
    ).json()
    assert [d["environment"] for d in deployments] == ["prod"], deployments


def test_a_target_naming_another_repos_agent_is_rejected_end_to_end(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # ADR-0091's sharpest edge, through the real webhook: `foreign-bot` belongs
    # to somebody else's repository, so this push must not deploy over it.
    _register(client, auth_headers, "two-agent-dev", "C000000D01")
    client.post(
        "/agents",
        json={
            "name": "foreign-bot",
            "channel": {"kind": "slack", "address": "C000000F01"},
            "repo_full_name": "someone-else/their-repo",
        },
        headers=auth_headers,
    )
    files = {
        **VALID_FILES,
        "deploy.yaml": (
            "targets:\n  prod:\n    agent: foreign-bot\n"
            "    env: prod\n    slack_channel: C000000F01\n"
        ),
    }
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, files)

    body = _post(client, "push", _push_payload("refs/heads/main", sha, clone_url)).json()
    assert body["status"] == "rejected", body
    assert body["errors"][0]["code"] == "deploy.agent_bound_elsewhere"

    # And nothing was deployed to the foreign agent.
    foreign = next(
        a for a in client.get("/agents", headers=auth_headers).json() if a["name"] == "foreign-bot"
    )
    assert (
        client.get("/deployments", params={"agent_id": foreign["id"]}, headers=auth_headers).json()
        == []
    )


def test_commit_poller_retries_after_routing_topology_is_repaired(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected sha deploys on the next pass after its target is created."""
    from curie_api.commitpoller import CommitPoller
    from curie_api.schemas import AgentCreate, ChannelBinding

    _register(client, auth_headers, "bootstrap_agent", "C000000R01")
    files = {
        **VALID_FILES,
        "deploy.yaml": (
            "targets:\n  dev:\n    agent: repairedagent\n"
            "    env: dev\n    slack_channel: C000000R02\n"
        ),
    }
    _, sha = _build_bare_repo(trusted_clone_base, REPO, files)
    settings = get_settings()

    class Tips:
        def sha_for(self, repo_full_name: str, branch: str) -> str | None:
            assert repo_full_name == REPO
            return sha if branch == settings.dev_branch else None

    class NoopEvalQueue:
        async def enqueue(self, _job: Any) -> None:
            pass

    async def exercise() -> str:
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        poller = CommitPoller(
            session_factory=maker,
            store=client.app.state.bundle_store,
            settings=settings,
            eval_queue=NoopEvalQueue(),
            tips=Tips(),
            interval_seconds=60,
        )
        try:
            first = await poller.poll_once()
            assert [move.sha for move in first] == [sha]
            assert "deploy.unknown_agent" in caplog.text

            async with maker() as session:
                assert await crud.list_deployments(session) == []
                repaired = await crud.create_agent(
                    session,
                    AgentCreate(
                        name="repairedagent",
                        channel=ChannelBinding(kind="slack", address="C000000R02"),
                        repo_full_name=REPO,
                    ),
                )

            second = await poller.poll_once()
            assert [move.sha for move in second] == [sha]
            return str(repaired.id)
        finally:
            await engine.dispose()

    repaired_id = asyncio.run(exercise())
    deployments = client.get(
        "/deployments", params={"agent_id": repaired_id}, headers=auth_headers
    ).json()
    assert len(deployments) == 1, deployments
    assert deployments[0]["commit_sha"] == sha
    assert deployments[0]["environment"] == "dev"


def test_the_commit_poller_promotes_a_stored_bundle_with_the_remote_deleted(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The polling lane inherits the reuse; it is not special-cased (#1211).

    The poller does not clone to notice a move -- it reads the branch tip from
    the GitHub API and then hands `process_push` the same payload the webhook
    would have delivered. So the two lanes must agree here, and the way to show
    that is to drive the real `CommitPoller` rather than to argue from shared
    code.

    Placement note: this lives here, beside the other real-poller test, and not
    in `test_commitpoller.py`. That file is deliberately pure-unit -- stubbed
    sessions, a stubbed `process_push`, no database, no object store and no bare
    repositories -- and this test needs all four. Adding them there would
    duplicate this module's fixtures into a file whose stated design is to not
    need them.

    The dev branch is already deployed at this sha, so the only move the pass
    finds is main: one promote, with no remote to promote from.
    """

    from curie_api.commitpoller import CommitPoller

    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)
    dev = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert dev["status"] == "deployed", dev

    _delete_bare_repo(trusted_clone_base)
    settings = get_settings()

    class Tips:
        """Both deploy branches sit on the pushed sha, as they do after a merge."""

        def sha_for(self, repo_full_name: str, branch: str) -> str | None:
            assert repo_full_name == REPO
            return sha

    class NoopEvalQueue:
        async def enqueue(self, _job: Any) -> None:
            pass

    spy = _ExtractSpy()
    monkeypatch.setattr(bundles, "extract_and_validate", spy)

    async def exercise() -> list[str]:
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        poller = CommitPoller(
            session_factory=maker,
            store=client.app.state.bundle_store,
            settings=settings,
            eval_queue=NoopEvalQueue(),
            tips=Tips(),
            interval_seconds=60,
        )
        try:
            return [move.branch for move in await poller.poll_once()]
        finally:
            await engine.dispose()

    moved = asyncio.run(exercise())
    assert moved == [settings.prod_branch], (
        f"only main has moved since the dev deploy; got {moved}"
    )
    assert spy.calls == 0, f"a polled promote must re-validate nothing; got {spy.calls}"

    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    prod_rows = [d for d in deployments if d["environment"] == "prod"]
    assert len(prod_rows) == 1, deployments
    assert prod_rows[0]["version_id"] == dev["version_id"]
    assert prod_rows[0]["commit_sha"] == sha


# --------------------------------------------------------------------------- #
# A scaffolded deploy.yaml declares nothing (#1210)
# --------------------------------------------------------------------------- #
# `curie init` writes a commented-out example plus a literal empty map, so the
# value below is the payload a fresh repository actually pushes, read from
# `DEPLOY_YAML` in cli/src/scaffold.rs rather than restated here.
SCAFFOLDED_DEPLOY_YAML = scaffolded_deploy_yaml()

SCAFFOLD_FILES = {**VALID_FILES, "deploy.yaml": SCAFFOLDED_DEPLOY_YAML}


def test_a_scaffolded_deploy_yaml_still_deploys(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    # #1210 through the real webhook: `curie init` scaffolds `targets: {}`, so
    # every first push from a scaffolded repository carried a deploy.yaml that
    # declared no routing, and the resolver ignored the push instead of
    # deploying to the one agent the repository binds. The unit tests reproduce
    # the deploy.yaml read by hand; this one runs the production read path
    # (gitflow._read_targets -> bundles.read_deploy_targets).
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, SCAFFOLD_FILES)

    body = _post(client, "push", _push_payload("refs/heads/dev", sha, clone_url)).json()
    assert body["status"] == "deployed", body
    assert body["deployment_id"] is not None
    assert body["agent_id"] == agent_id


def test_a_scaffolded_deploy_yaml_reads_back_as_an_empty_map(tmp_path: Path) -> None:
    # The premise the whole of #1210 rests on, pinned directly. None from
    # `read_deploy_targets` means the file is ABSENT; a non-null file whose
    # `targets` map is empty means it is PRESENT and declares nothing.
    # Conflating the two is the bug: had a scaffolded deploy.yaml read back as
    # None, the old `if targets is None:` gate would already have deployed it.
    for rel, content in SCAFFOLD_FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    targets = bundles.read_deploy_targets(tmp_path)
    assert targets is not None, "a present deploy.yaml must not read back as absent"
    assert targets.targets == {}
