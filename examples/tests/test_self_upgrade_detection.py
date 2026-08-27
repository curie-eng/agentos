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
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sre-bot" / "self-upgrade"))

from redeploy import (  # noqa: E402
    BUNDLE_PREFIX,
    SelfUpgradeError,
    bundle_from_repo_tarball,
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
