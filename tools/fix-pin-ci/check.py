"""Apply the pull request fix pin declaration policy."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
DECLARATION = re.compile(r"Fix pin: (?P<selector>\S+)")
DECLARATION_ATTEMPT = re.compile(
    r"^\s*(?:(?:[>#*_~`+-]+|\d+[.)]|\[[ x]\])\s*)*+fix[^\w\r\n]+pin\s*(?::|[-=])",
    re.IGNORECASE,
)
SELECTOR = re.compile(
    r"(?:"
    r"(?:apps|packages)/[A-Za-z0-9_-]+/tests/"
    r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py::"
    r"(?:[A-Za-z_][A-Za-z0-9_]*::)*"
    r"test[A-Za-z0-9_]*(?:\[[A-Za-z0-9_.-]+\])?"
    r"|cli/tests/[A-Za-z0-9_-]+\.rs::[A-Za-z_][A-Za-z0-9_]*"
    r"|charts/curie/ci/[A-Za-z0-9_-]+\.sh"
    r")"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--curie", required=True)
    parser.add_argument("--ref", required=True)
    return parser


def _body(event_path: Path) -> str:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read pull request event: {error}") from error

    if not isinstance(event, dict):
        raise ValueError("pull request event must be an object")

    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        return ""

    body = pull_request.get("body")
    if body is None:
        return ""
    if not isinstance(body, str):
        raise ValueError("pull request body must be a string or null")
    return body


def _selector(body: str) -> str | None:
    uncommented = COMMENT.sub("", body)
    attempts = [
        line for line in uncommented.splitlines() if DECLARATION_ATTEMPT.search(line)
    ]
    if not attempts:
        return None

    if len(attempts) != 1 or (match := DECLARATION.fullmatch(attempts[0])) is None:
        raise ValueError("Fix pin declaration must be exactly one unindented selector line")

    selector = match.group("selector")
    if SELECTOR.fullmatch(selector) is None:
        raise ValueError(f"unsupported Fix pin selector: {selector}")
    return selector


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)

    try:
        selector = _selector(_body(arguments.event))
    except ValueError as error:
        print(f"Fix pin declaration error: {error}", file=sys.stderr)
        return 1

    if selector is None:
        print("SKIPPED: no Fix pin declaration")
        return 0

    completed = subprocess.run(
        [arguments.curie, "dev", "verify-fix-pin", arguments.ref, selector],
        capture_output=True,
        check=False,
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(completed.stderr)
    sys.stderr.buffer.flush()

    if completed.returncode != 0:
        return completed.returncode
    if b"PINNED" not in completed.stdout.splitlines():
        print("Fix pin verification error: verifier did not report PINNED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
