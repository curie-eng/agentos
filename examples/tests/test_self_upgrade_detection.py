"""The self-upgrade job's two pure decisions, tested without a network.

Both are cheap to get subtly wrong in the direction that reports success:

* "am I behind" has three answers, not two. A version deployed by hand from a
  working copy records no commit, and that must read as UNKNOWN rather than as
  up to date -- otherwise the job goes quiet forever on exactly the install where
  someone deployed once by hand.
* re-taring a subdirectory out of a repository tarball has to drop everything
  outside it, including symlinks, which are how a tar extraction escapes.
"""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sre-bot" / "self-upgrade"))

from redeploy import (  # noqa: E402
    deploy,
    BUNDLE_PREFIX,
    SelfUpgradeError,
    bundle_from_repo_tarball,
    deployed_commit,
    member_of,
    pin_build_connectors,
    replace_member,
)


def _repo_tarball(files: dict[str, bytes], root: str = "curie-eng-curie-abc1234") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _names(bundle: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


def test_the_bundle_is_lifted_out_of_the_repository_tarball() -> None:
    bundle = bundle_from_repo_tarball(
        _repo_tarball(
            {
                f"{BUNDLE_PREFIX}/.claude-plugin/plugin.json": b"{}",
                f"{BUNDLE_PREFIX}/skills/sre-bot/SKILL.md": b"# skill",
                f"{BUNDLE_PREFIX}/evals/cases.json": b"[]",
            }
        )
    )
    assert _names(bundle) == [
        ".claude-plugin/plugin.json",
        "evals/cases.json",
        "skills/sre-bot/SKILL.md",
    ]


def test_everything_outside_the_bundle_is_left_behind() -> None:
    # The repository is a monorepo: the platform's own source sits beside the
    # bundle and must not be packaged into an agent version.
    bundle = bundle_from_repo_tarball(
        _repo_tarball(
            {
                f"{BUNDLE_PREFIX}/.claude-plugin/plugin.json": b"{}",
                "apps/api/src/curie_api/main.py": b"# not the bundle",
                "README.md": b"# not the bundle",
                "examples/weather/connectors.yaml": b"# another bundle",
            }
        )
    )
    assert _names(bundle) == [".claude-plugin/plugin.json"]


def test_the_sha_carrying_root_is_discovered_not_assumed() -> None:
    # GitHub names the top-level directory after the ref, so it cannot be hardcoded.
    bundle = bundle_from_repo_tarball(
        _repo_tarball(
            {f"{BUNDLE_PREFIX}/.claude-plugin/plugin.json": b"{}"},
            root="curie-eng-curie-deadbeefcafe",
        )
    )
    assert _names(bundle) == [".claude-plugin/plugin.json"]


def test_an_empty_result_is_an_error_rather_than_an_empty_bundle() -> None:
    # Uploading an empty bundle would succeed and leave the agent with nothing.
    with pytest.raises(SelfUpgradeError, match="no bundle files"):
        bundle_from_repo_tarball(_repo_tarball({"README.md": b"# only this"}))


def test_a_symlink_inside_the_bundle_is_dropped() -> None:
    out = io.BytesIO()
    root = "curie-eng-curie-abc1234"
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{root}/{BUNDLE_PREFIX}/.claude-plugin/plugin.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
        link = tarfile.TarInfo(f"{root}/{BUNDLE_PREFIX}/skills/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../etc/passwd"
        tar.addfile(link)
    assert _names(bundle_from_repo_tarball(out.getvalue())) == [".claude-plugin/plugin.json"]


# --- pinning images from the commit being deployed ---------------------------
#
# The repository declares `build:` for its connectors, which records a LOCAL
# image id the cluster tier refuses. `release.yaml` publishes each connector on
# every release-branch push tagged `sha-<commit>`, so the images that belong with
# a bundle are derivable from the bundle's own commit.
#
# The second substitution is the one that is easy to forget: the declaration in
# the repository ships PLACEHOLDER allowlists, and deploying those verbatim would
# leave every write connector refusing every call -- which reads exactly like a
# working bot that has decided not to act.


def _bundle(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def _read(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()
            for member in tar.getmembers()
            if member.isfile()
        }


DECLARATION = b"""connectors:
  kubernetes:
    image: ghcr.io/containers/kubernetes-mcp-server@sha256:aaa
  k8s-write:
    build:
      context: connectors/k8s-write
    env:
      K8S_WRITE_ALLOWLIST: <namespace>/<deployment>
"""


@pytest.fixture
def offline_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No registry in a unit test; the resolution itself is exercised live."""
    import redeploy

    monkeypatch.setattr(redeploy, "resolve_digest", lambda _c, _commit: "sha256:fixture")


def test_a_build_connector_is_pinned_to_the_commits_published_image(
    offline_registry: None,
) -> None:
    parsed = yaml.safe_load(pin_build_connectors(DECLARATION, "c" * 40, {"k8s-write": {}}))
    write = parsed["connectors"]["k8s-write"]
    assert "build" not in write, "a build declaration cannot reach a cluster deploy"
    assert write["image"] == ("ghcr.io/curie-eng/curie-sre-bot-k8s-write@sha256:fixture")
    # An already-pinned connector is left alone rather than re-resolved.
    assert parsed["connectors"]["kubernetes"]["image"].endswith("@sha256:aaa")


def test_the_running_ceiling_survives_the_upgrade(offline_registry: None) -> None:
    # The placeholder in the repository would refuse every call, and a bot that
    # refuses everything looks exactly like a bot that chose not to act.
    parsed = yaml.safe_load(
        pin_build_connectors(
            DECLARATION, "c" * 40, {"k8s-write": {"K8S_WRITE_ALLOWLIST": "ns/one,ns/two"}}
        )
    )
    assert parsed["connectors"]["k8s-write"]["env"]["K8S_WRITE_ALLOWLIST"] == "ns/one,ns/two"


def test_replacing_one_member_leaves_the_others_byte_for_byte() -> None:
    original = _bundle({"connectors.yaml": b"old", "skills/sre-bot/SKILL.md": b"the skill"})
    swapped = _read(replace_member(original, "connectors.yaml", b"new"))
    assert swapped["connectors.yaml"] == b"new"
    assert swapped["skills/sre-bot/SKILL.md"] == b"the skill"


def test_a_missing_member_is_an_error_rather_than_an_empty_file() -> None:
    with pytest.raises(SelfUpgradeError):
        member_of(_bundle({"skills/sre-bot/SKILL.md": b"x"}), "connectors.yaml")


# --- the version being SERVED, not the newest row -----------------------------
#
# Those are different facts. A version can be created and never deployed, and
# reading that one made the job ask for a connector surface that does not exist
# -- "no bundle stored for this version", which reads like a broken agent rather
# than a question asked about the wrong row.


class _FakeApi:
    """Answers the three GETs deployed_commit makes, in order."""

    def __init__(self, deployments: list[dict], versions: list[dict]) -> None:
        self.routes = {
            "/agents": [{"id": "agent-1", "name": "sre-bot"}],
            "/deployments": deployments,
            "/agents/agent-1/versions": versions,
        }

    def __call__(self, request, timeout=0):  # noqa: ANN001 - urlopen's shape
        path = request.full_url.replace("http://api", "")
        import io as _io

        return _io.BytesIO(json.dumps(self.routes[path]).encode())


def _patched(monkeypatch: pytest.MonkeyPatch, api: _FakeApi) -> None:
    import redeploy

    class _Ctx:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self.body

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(redeploy.urllib.request, "urlopen", lambda r, timeout=0: _Ctx(api(r)))


def test_the_served_version_wins_over_a_newer_undeployed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi(
        deployments=[
            {
                "agent_id": "agent-1",
                "version_id": "served",
                "status": "active",
                "deployed_at": "2026-01-01T00:00:00",
                "commit_sha": None,
            }
        ],
        versions=[
            {"id": "served", "commit_sha": "a" * 40, "created_at": "2026-01-01T00:00:00"},
            # Newer, and never deployed: exactly the row that used to win.
            {"id": "never-deployed", "commit_sha": "b" * 40, "created_at": "2026-06-01T00:00:00"},
        ],
    )
    _patched(monkeypatch, api)

    agent_id, commit, version_id = deployed_commit("http://api", "k", "sre-bot")

    assert version_id == "served"
    assert commit == "a" * 40


def test_no_active_deployment_reads_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never "up to date". A job that cannot tell must not report success.
    api = _FakeApi(deployments=[], versions=[{"id": "v", "commit_sha": "c" * 40}])
    _patched(monkeypatch, api)
    _agent, commit, version_id = deployed_commit("http://api", "k", "sre-bot")
    assert commit is None and version_id is None


# --- the bundle upload is a form, not a body ---------------------------------
#
# The endpoint is an upload rather than a document write. A raw PUT is refused
# with a 422 naming a field the caller never knew about:
#     {"loc": ["body", "file"], "msg": "Field required"}
# It failed exactly there on the live install, after the job had already fetched
# the bundle, resolved three image digests and created the version -- so the cost
# of getting this wrong is a version row with no bundle behind it.


def test_the_bundle_is_uploaded_as_a_multipart_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redeploy

    seen: list[dict] = []

    def _fake(api_url, api_key, path, *, method="GET", body=None, content_type="application/json"):
        seen.append({"path": path, "method": method, "body": body, "content_type": content_type})
        if path.endswith("/versions"):
            return json.dumps({"id": "v-1"}).encode()
        return b"{}"

    monkeypatch.setattr(redeploy, "_api", _fake)
    deploy("http://api", "k", "agent-1", b"BUNDLEBYTES", "d" * 40)

    upload = next(c for c in seen if c["path"].endswith("/bundle"))
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    boundary = upload["content_type"].split("boundary=", 1)[1]
    assert upload["body"].startswith(f"--{boundary}\r\n".encode())
    assert b'name="file"; filename="bundle.tar.gz"' in upload["body"]
    assert b"BUNDLEBYTES" in upload["body"], "the bundle itself must survive the framing"
    assert upload["body"].endswith(f"\r\n--{boundary}--\r\n".encode())

    # And the order still holds: create, upload, then deploy -- so a failure
    # never leaves a version marked active with no bundle behind it.
    assert [c["path"].rsplit("/", 1)[-1] for c in seen] == ["versions", "bundle", "deployments"]
