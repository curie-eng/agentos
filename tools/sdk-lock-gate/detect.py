"""Detect whether a ``uv.lock`` diff changes what is resolved as ``claude-agent-sdk`` (#2094).

The live approval-gate proof in the e2e ladder is the only thing that exercises a real
model against a real SDK permission dispatch. It is expensive, so it cannot run on every
pull request -- but it MUST run whenever the SDK bytes underneath it move. This module is
the trigger: it compares the base and head ``uv.lock`` and answers one question, "did the
resolved ``claude-agent-sdk`` entry change?".

The two error directions do not cost the same. A false positive costs one live ladder run.
A false negative lets a new SDK build reach production without the approval gate ever
being re-proven against it -- the exact escape #2094 exists to close. So the rule is
deliberately over-triggering: the whole ``[[package]]`` table is fingerprinted (version,
``source``, ``dependencies``, ``sdist``, ``wheels``), and so is every other line naming the
SDK. A same-version wheel re-upload, a hash change, or a transitive dependency swap all
mean different bytes execute the permission dispatch, and all of them return ``True``.

For the same reason the detector fails **open**: a missing side or a lock that is not valid
TOML returns ``True``. A corrupt lock is somebody else's CI failure and must never silently
disarm this gate.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

SDK_NAME = "claude-agent-sdk"
PACKAGE_HEADER = "[[package]]"

# (parsed SDK tables, sorted other lines naming the SDK). Comparable, not hashable:
# the tables stay nested dicts and are compared with ``==``, never hashed.
Fingerprint = tuple[list[dict[str, Any]], tuple[str, ...]]


def _sdk_tables(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``[[package]]`` tables whose ``name`` is exactly ``claude-agent-sdk``.

    The match is equality on the parsed ``name`` key, never a substring of the file text,
    so a distinct package such as ``claude-agent-sdk-extras`` is not mistaken for the SDK.
    """
    packages = document.get("package")
    if not isinstance(packages, list):
        return []
    return [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == SDK_NAME
    ]


def _sections(text: str) -> list[list[str]]:
    """Split the lock into TOML sections, each starting at a header line.

    A header is a line that begins at column zero with ``[`` and ends with ``]``:
    ``[[package]]`` or ``[package.metadata]``. Array continuation lines are indented and
    the closing ``]`` of an array does not begin with ``[``, so neither is misread.
    """
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.startswith("[") and line.rstrip().endswith("]"):
            sections.append([])
        sections[-1].append(line)
    return sections


def _back_reference_lines(text: str) -> tuple[str, ...]:
    """Return the sorted stripped lines naming the SDK from outside its own table.

    This is the second half of the fingerprint. The parsed table alone does not see the
    ``requires-dist`` specifier in ``[package.metadata]`` or the ``dependencies``
    back-reference from ``curie-runner``: both can move while the resolved table stays
    byte-identical, and both change which SDK build a fresh resolve would pick. Collecting
    the raw lines catches them without modelling the whole dependency graph.

    Lines inside the SDK's own ``[[package]]`` table are dropped because the first half
    already covers them. The section scan is a heuristic; if it ever fails to recognise the
    table, those lines are merely counted twice, which cannot turn a change into a
    non-change.
    """
    own_name = f'name = "{SDK_NAME}"'
    collected: list[str] = []
    for section in _sections(text):
        if not section:
            continue
        is_sdk_table = section[0].strip() == PACKAGE_HEADER and any(
            line.strip() == own_name for line in section
        )
        if is_sdk_table:
            continue
        collected.extend(line.strip() for line in section if SDK_NAME in line)
    return tuple(sorted(collected))


def _fingerprint(text: str | None) -> Fingerprint | None:
    """Fingerprint one side of the diff, or ``None`` when it cannot be read.

    ``None`` means "unknown", and every unknown side fails open in
    :func:`sdk_entry_changed`.
    """
    if text is None:
        return None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        print(
            f"sdk-lock-gate: uv.lock is not valid TOML ({error}); failing open",
            file=sys.stderr,
        )
        return None
    return _sdk_tables(document), _back_reference_lines(text)


def sdk_entry_changed(old_text: str | None, new_text: str | None) -> bool:
    """Report whether the resolved ``claude-agent-sdk`` entry differs between two locks.

    ``old_text`` and ``new_text`` are the full ``uv.lock`` contents on the base and head
    refs, or ``None`` when the file is absent on that ref. Returns ``True`` when either
    side is unknown or malformed, and otherwise when the two fingerprints differ.

    There is deliberately no ``old_text == new_text`` shortcut: identical text is already
    an identical fingerprint, and a shortcut would skip the parse that reports a malformed
    lock.
    """
    old = _fingerprint(old_text)
    new = _fingerprint(new_text)
    if old is None or new is None:
        return True
    return old != new


def _read(path: Path) -> str | None:
    """Read a lock file, treating an absent path as absent on that ref."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-file", required=True, type=Path)
    parser.add_argument("--new-file", required=True, type=Path)
    return parser


def _run() -> None:
    args = _parser().parse_args()
    changed = sdk_entry_changed(_read(args.old_file), _read(args.new_file))

    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise ValueError("GITHUB_OUTPUT is required")
    with Path(output_path).open("a", encoding="utf-8") as stream:
        stream.write(f"changed={'true' if changed else 'false'}\n")


def main() -> int:
    """Write the verdict and always exit 0; the workflow reads the output, not the code."""
    _run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
