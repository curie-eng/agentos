"""Executable contract for the ``claude-agent-sdk`` lock-entry detector (#2094).

The detector answers one question for a PR workflow: did this ``uv.lock`` diff
change what the runner will actually import as ``claude-agent-sdk``? A ``True``
answer costs one live ladder run against a real provider. A ``False`` answer
costs nothing -- and, if it is wrong, lets a new SDK build reach production
without the live approval gate ever being re-proven, which is the exact escape
#2094 exists to close.

The two costs are not symmetric, so the rule is deliberately over-triggering:
the whole ``[[package]]`` table is fingerprinted (version, ``source``,
``dependencies``, ``sdist``, ``wheels``), plus every other line in the file
naming ``claude-agent-sdk`` -- the back-reference and the ``requires-dist``
specifier. A same-version wheel re-upload, a hash change, or a transitive
dependency swap all mean different SDK bytes execute the permission dispatch
that PR #2068 rewired, so all of them must return ``True``. Malformed TOML and
a missing side fail **open** for the same reason: a corrupt lock is somebody
else's CI failure and must never silently disarm this gate.

The fixtures below are cut down from the real ``uv.lock`` in this repo (the
``claude-agent-sdk`` table at ``uv.lock:604`` and its two back-references at
``:837`` and ``:849``), so the shapes the detector parses here are the shapes
it meets in production.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
DETECTOR = REPO_ROOT / "tools" / "sdk-lock-gate" / "detect.py"


@cache
def _detector() -> ModuleType:
    """Load ``detect.py`` by path: ``tools/*`` holds scripts, not packages."""
    spec = importlib.util.spec_from_file_location("curie_sdk_lock_gate", DETECTOR)
    assert spec is not None and spec.loader is not None, f"cannot load {DETECTOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def changed(old_text: str | None, new_text: str | None) -> bool:
    """Call the detector and pin that it returns a real ``bool``, not a truthy proxy."""
    result = _detector().sdk_entry_changed(old_text, new_text)
    assert isinstance(result, bool), f"sdk_entry_changed returned {type(result)!r}, not bool"
    return result


# --- fixtures ---------------------------------------------------------------
# Trimmed from the real uv.lock: fewer wheels and fewer packages, but the same
# table shapes, the same key order, and both claude-agent-sdk back-references.

BASE_LOCK = """version = 1
revision = 3
requires-python = ">=3.13"

[[package]]
name = "anyio"
version = "4.12.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "idna" },
]
sdist = { url = "https://files.pythonhosted.org/packages/aa/anyio-4.12.0.tar.gz", \
hash = "sha256:1111111111111111111111111111111111111111111111111111111111111111", size = 219823 }
wheels = [
    { url = "https://files.pythonhosted.org/packages/bb/anyio-4.12.0-py3-none-any.whl", \
hash = "sha256:2222222222222222222222222222222222222222222222222222222222222222", size = 107213 },
]

[[package]]
name = "claude-agent-sdk"
version = "0.2.135"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "anyio" },
    { name = "mcp" },
    { name = "sniffio" },
]
sdist = { url = "https://files.pythonhosted.org/packages/d8/claude_agent_sdk-0.2.135.tar.gz", \
hash = "sha256:471ae3769d7814c658fa0a37dbd95bb4b1e365563d6a794dcdd99586a29ec53b", size = 312219 }
wheels = [
    { url = "https://files.pythonhosted.org/packages/a6/\
claude_agent_sdk-0.2.135-py3-none-manylinux_2_17_aarch64.whl", \
hash = "sha256:5306f142ea018eca519cb7d0c3d7b4e97702ae009285d9e1c4896357fe87e27c", size = 92281562 },
    { url = "https://files.pythonhosted.org/packages/1b/\
claude_agent_sdk-0.2.135-py3-none-manylinux_2_17_x86_64.whl", \
hash = "sha256:01a4ded2b5b19edf2395ab77a38ce0d0e6cc0d6e1a263f85c701de5eb3764e20", size = 93366910 },
]

[[package]]
name = "claude-agent-sdk-extras"
version = "0.1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/cc/\
claude_agent_sdk_extras-0.1.0-py3-none-any.whl", \
hash = "sha256:3333333333333333333333333333333333333333333333333333333333333333", size = 4211 },
]

[[package]]
name = "click"
version = "8.5.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/c7/click-8.5.0.tar.gz", \
hash = "sha256:ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34", size = 382235 }
wheels = [
    { url = "https://files.pythonhosted.org/packages/58/click-8.5.0-py3-none-any.whl", \
hash = "sha256:255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360", size = 125251 },
]

[[package]]
name = "curie-runner"
version = "0.0.0"
source = { editable = "runner" }
dependencies = [
    { name = "anyio" },
    { name = "claude-agent-sdk" },
]

[package.metadata]
requires-dist = [
    { name = "anyio", specifier = ">=4.6" },
    { name = "claude-agent-sdk", specifier = ">=0.2.135" },
]
"""


def without_sdk(text: str) -> str:
    """Drop the SDK's own table and both of its back-references.

    Models the shape uv actually writes when the dependency is dropped from
    ``runner/pyproject.toml``: the resolved entry disappears *and* so do the
    lines naming it elsewhere.
    """
    exact = '"claude-agent-sdk"'
    kept: list[str] = []
    for block in text.split("\n\n"):
        if f"\nname = {exact}\n" in f"\n{block}\n":
            continue
        kept.append("\n".join(line for line in block.splitlines() if exact not in line))
    return "\n\n".join(kept)


# --- the rule ---------------------------------------------------------------


def test_an_sdk_version_bump_is_reported_as_changed() -> None:
    new = BASE_LOCK.replace('version = "0.2.135"', 'version = "0.2.140"')
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is True


def test_an_untouched_lock_file_is_reported_as_unchanged() -> None:
    assert changed(BASE_LOCK, BASE_LOCK) is False


def test_reformatting_an_unrelated_package_is_reported_as_unchanged() -> None:
    """Whitespace and key order elsewhere are not SDK bytes."""
    new = BASE_LOCK.replace(
        '[[package]]\nname = "click"\nversion = "8.5.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        '[[package]]\n\nversion   = "8.5.0"\nname = "click"\n'
        'source = {registry = "https://pypi.org/simple"}\n',
    )
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is False


def test_bumping_an_unrelated_package_is_reported_as_unchanged() -> None:
    """The whole point of the gate: a routine dependency bump costs no live run."""
    new = BASE_LOCK.replace('version = "8.5.0"', 'version = "8.6.0"').replace(
        "click-8.5.0", "click-8.6.0"
    )
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is False


def test_a_republished_sdk_wheel_at_the_same_version_is_reported_as_changed() -> None:
    """Same version, different bytes -- the case a version-only rule would miss."""
    new = BASE_LOCK.replace(
        "sha256:5306f142ea018eca519cb7d0c3d7b4e97702ae009285d9e1c4896357fe87e27c",
        "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    )
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is True


def test_a_change_to_the_sdks_own_dependencies_is_reported_as_changed() -> None:
    new = BASE_LOCK.replace('    { name = "sniffio" },\n', "")
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is True


def test_a_requires_dist_specifier_change_is_reported_as_changed() -> None:
    """The resolved table is byte-identical; only the back-reference moved."""
    new = BASE_LOCK.replace('specifier = ">=0.2.135"', 'specifier = ">=0.2.140"')
    assert new != BASE_LOCK
    assert '\nversion = "0.2.135"\n' in new
    assert changed(BASE_LOCK, new) is True


def test_adding_the_sdk_to_the_lock_is_reported_as_changed() -> None:
    assert changed(without_sdk(BASE_LOCK), BASE_LOCK) is True


def test_removing_the_sdk_from_the_lock_is_reported_as_changed() -> None:
    assert changed(BASE_LOCK, without_sdk(BASE_LOCK)) is True


def test_a_lock_without_the_sdk_on_either_side_is_reported_as_unchanged() -> None:
    """No SDK anywhere means nothing this gate defends can have moved."""
    old = without_sdk(BASE_LOCK)
    new = old.replace('version = "8.5.0"', 'version = "8.6.0"')
    assert '"claude-agent-sdk"' not in old
    assert new != old
    assert changed(old, new) is False


def test_a_similarly_named_package_is_not_mistaken_for_the_sdk() -> None:
    """``claude-agent-sdk-extras`` is a different package: exact ``name`` match."""
    new = BASE_LOCK.replace('version = "0.1.0"', 'version = "0.2.0"').replace(
        "claude_agent_sdk_extras-0.1.0", "claude_agent_sdk_extras-0.2.0"
    )
    assert new != BASE_LOCK
    assert changed(BASE_LOCK, new) is False


# --- fail-open --------------------------------------------------------------


def test_a_malformed_lock_file_fails_open() -> None:
    """A corrupt lock is a separate CI failure; it must not disarm this gate."""
    assert changed(BASE_LOCK, BASE_LOCK + '\n[[package\nname = "broken\n') is True


def test_a_missing_base_side_fails_open() -> None:
    """``uv.lock`` absent on the base ref: nothing to compare, so run the proof."""
    assert changed(None, BASE_LOCK) is True


# --- the CLI ----------------------------------------------------------------


def test_the_cli_writes_the_verdict_to_github_output(tmp_path: Path) -> None:
    """The only surface the workflow reads: ``changed=true`` / ``changed=false``."""
    old_file = tmp_path / "old.lock"
    new_file = tmp_path / "new.lock"
    old_file.write_text(BASE_LOCK, encoding="utf-8")

    def run(new_text: str, output_name: str) -> str:
        new_file.write_text(new_text, encoding="utf-8")
        github_output = tmp_path / output_name
        env = dict(os.environ, GITHUB_OUTPUT=str(github_output))
        result = subprocess.run(
            [
                sys.executable,
                str(DETECTOR),
                "--old-file",
                str(old_file),
                "--new-file",
                str(new_file),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return github_output.read_text(encoding="utf-8")

    bumped = BASE_LOCK.replace('version = "0.2.135"', 'version = "0.2.140"')
    assert "changed=true" in run(bumped, "bumped.txt")

    unrelated = BASE_LOCK.replace('version = "8.5.0"', 'version = "8.6.0"')
    assert "changed=false" in run(unrelated, "unrelated.txt")
