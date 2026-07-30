"""Git-flow end to end against real Postgres + MinIO with local bare repos.

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
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from curie_api import crud
from curie_api.config import get_settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

SECRET = get_settings().github_webhook_secret
REPO = "octo/demo-agent"

VALID_FILES = {
    ".claude-plugin/plugin.json": '{"name": "demo-plugin", "version": "0.1.0"}',
    "skills/alpha/SKILL.md": "---\nname: alpha\ndescription: does alpha\n---\n",
    "skills/beta/SKILL.md": "---\nname: beta\ndescription: does beta\n---\n",
}


@pytest.fixture
def trusted_clone_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
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


def _build_bare_repo(
    base_dir: Path, repo_full_name: str, files: dict[str, str]
) -> tuple[str, str]:
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
    return f"file://{bare}", sha


def _post(
    client: Any, event: str, payload: dict[str, Any], secret: str = SECRET
) -> Any:
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


def _register_agent(client: Any, headers: dict[str, str]) -> str:
    agent = client.post(
        "/agents",
        json={
            "name": "gitflow-agent",
            "slack_channel": "C000000G01",
            "repo_full_name": REPO,
        },
        headers=headers,
    ).json()
    return str(agent["id"])


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
    version = client.get(
        f"/agents/{agent_id}/versions", headers=auth_headers
    ).json()[0]
    assert version["commit_sha"] == sha
    assert version["bundle_ref"] is not None
    bundle = client.get(
        f"/agents/{agent_id}/versions/{version['id']}/bundle", headers=auth_headers
    )
    assert bundle.status_code == 200
    assert len(bundle.content) > 0

    # The deployment routes to the dev bot.
    deployments = client.get(
        "/deployments", params={"agent_id": agent_id}, headers=auth_headers
    ).json()
    assert len(deployments) == 1
    assert deployments[0]["environment"] == "dev"


def test_main_push_promotes_and_reuses_the_built_version(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    trusted_clone_base: Path,
) -> None:
    agent_id = _register_agent(client, auth_headers)
    clone_url, sha = _build_bare_repo(trusted_clone_base, REPO, VALID_FILES)

    dev = _post(
        client, "push", _push_payload("refs/heads/dev", sha, clone_url)
    ).json()
    prod = _post(
        client, "push", _push_payload("refs/heads/main", sha, clone_url)
    ).json()

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

    versions = client.get(
        f"/agents/{agent_id}/versions", headers=auth_headers
    ).json()
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


def test_ping_event_pongs(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
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
    resp = _post(
        client, "push", _push_payload("refs/heads/feature-x", sha, clone_url)
    )
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
        _push_payload(
            "refs/heads/dev", sha, f"https://evil.example/{REPO}.git"
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rejected"
    codes = {e["code"] for e in body["errors"]}
    assert "git.origin_mismatch" in codes

    # Nothing was built and nothing was deployed.
    assert client.get(f"/agents/{agent_id}/versions", headers=auth_headers).json() == []
    assert (
        client.get(
            "/deployments", params={"agent_id": agent_id}, headers=auth_headers
        ).json()
        == []
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
        client.get(
            "/deployments", params={"agent_id": agent_id}, headers=auth_headers
        ).json()
        == []
    )
