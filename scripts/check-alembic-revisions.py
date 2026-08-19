import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from alembic.script import ScriptDirectory

FILENAME_PATTERN = re.compile(r"^(\d+)_.*\.py$")
DEFAULT_SCRIPT_LOCATION = (
    Path(__file__).resolve().parents[1] / "apps" / "api" / "alembic"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Alembic revision numbers and graph heads."
    )
    parser.add_argument(
        "--script-location",
        type=Path,
        default=DEFAULT_SCRIPT_LOCATION,
        help="Alembic script directory to validate.",
    )
    args = parser.parse_args()
    script_location: Path = args.script_location
    versions = script_location / "versions"

    if not script_location.is_dir():
        print(
            f"Alembic revision gate failed: script location does not exist: "
            f"{script_location}",
            file=sys.stderr,
        )
        return 1

    if not versions.is_dir():
        print(
            f"Alembic revision gate failed: versions directory does not exist: "
            f"{versions}",
            file=sys.stderr,
        )
        return 1

    filenames_by_number: dict[str, list[str]] = defaultdict(list)
    try:
        for path in versions.iterdir():
            if not path.is_file():
                continue
            match = FILENAME_PATTERN.fullmatch(path.name)
            if match is not None:
                filenames_by_number[match.group(1)].append(path.name)
    except OSError as exc:
        print(
            f"Alembic revision gate failed: could not scan versions directory "
            f"{versions}: {exc}",
            file=sys.stderr,
        )
        return 1

    duplicates = {
        number: sorted(filenames)
        for number, filenames in filenames_by_number.items()
        if len(filenames) > 1
    }
    if duplicates:
        print(
            "Alembic revision gate failed: duplicate numeric revision "
            "filename tokens found:",
            file=sys.stderr,
        )
        for number in sorted(duplicates):
            print(
                f"  {number}: {', '.join(duplicates[number])}",
                file=sys.stderr,
            )
        print(
            "Rename migrations so every leading numeric token is unique.",
            file=sys.stderr,
        )
        return 1

    try:
        heads = sorted(ScriptDirectory(str(script_location)).get_heads())
    except Exception as exc:
        print(
            f"Alembic revision gate failed: could not load the revision graph "
            f"from {script_location}: {exc}",
            file=sys.stderr,
        )
        print(
            "Fix malformed revision modules and graph references, then rerun "
            "the checker.",
            file=sys.stderr,
        )
        return 1

    if len(heads) != 1:
        rendered_heads = ", ".join(heads) if heads else "none"
        print(
            f"Alembic revision gate failed: expected exactly one Alembic head, "
            f"found {len(heads)}: {rendered_heads}",
            file=sys.stderr,
        )
        print(
            "Create a merge revision so the migration tree has one head.",
            file=sys.stderr,
        )
        return 1

    print(f"Alembic revision gate passed with head {heads[0]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
