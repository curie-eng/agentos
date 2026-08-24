#!/usr/bin/env python3
"""Clone an architecture-atlas snapshot and register a new version."""

from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import date
from pathlib import Path

VERSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(
        description="Copy a prior atlas snapshot into a new registered version."
    )
    parser.add_argument("--version", required=True, help="Version id, for example v0.7.2")
    parser.add_argument("--commit", required=True, help="Full 40-character git commit")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--label")
    parser.add_argument("--release")
    parser.add_argument("--from-version")
    parser.add_argument(
        "--atlas-dir",
        type=Path,
        default=repo_root / "docs" / "architecture-atlas",
    )
    return parser.parse_args()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if not VERSION_ID.fullmatch(args.version):
        raise SystemExit("--version may contain only letters, digits, dot, underscore, and hyphen")
    if not COMMIT.fullmatch(args.commit):
        raise SystemExit("--commit must be a lowercase 40-character git commit")

    atlas_dir = args.atlas_dir.resolve()
    manifest_path = atlas_dir / "versions.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    versions = manifest["versions"]
    if any(item["id"] == args.version for item in versions):
        raise SystemExit(f"version already exists: {args.version}")

    base_id = args.from_version or manifest["defaultVersion"]
    try:
        base = next(item for item in versions if item["id"] == base_id)
    except StopIteration as error:
        raise SystemExit(f"base version is not registered: {base_id}") from error

    base_path = (atlas_dir / base["file"]).resolve()
    if atlas_dir not in base_path.parents or not base_path.is_file():
        raise SystemExit(f"base snapshot is missing or escapes the atlas: {base['file']}")

    relative_target = Path("snapshots") / f"{args.version}.json"
    target_path = atlas_dir / relative_target
    if target_path.exists():
        raise SystemExit(f"snapshot file already exists: {target_path}")

    snapshot = copy.deepcopy(json.loads(base_path.read_text(encoding="utf-8")))
    snapshot["asOf"] = args.date
    snapshot["repository"].update(
        {
            "branch": args.branch,
            "commit": args.commit,
            "shortCommit": args.commit[:8],
            "release": args.release or args.version,
        }
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(target_path, snapshot)

    versions.append(
        {
            "id": args.version,
            "label": args.label or args.version,
            "file": relative_target.as_posix(),
            "date": args.date,
            "branch": args.branch,
            "commit": args.commit,
        }
    )
    manifest["defaultVersion"] = args.version
    write_json(manifest_path, manifest)

    print(f"created {target_path}")
    print(f"registered {args.version} as the default in {manifest_path}")
    print(f"compare {base['commit']}..{args.commit}, then update only evidence-backed changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
