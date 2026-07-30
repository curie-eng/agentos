"""Where a plugin bundle's manifest lives, and how to find it.

The Claude Code plugin manifest is ``.claude-plugin/plugin.json``; a bare
``plugin.json`` at the bundle root is accepted as a fallback (see the package
README's Decisions). This module is the single owner of that ordered lookup so
validation, archive handling, bundle inspection, and the runner readers cannot
drift on the locations or their precedence. It is a location policy only: it
makes no schema, wire, or protocol change.
"""

from pathlib import Path

# Ordered manifest locations, most-specific first. ``.claude-plugin/plugin.json``
# is canonical; a root ``plugin.json`` is the accepted fallback.
MANIFEST_LOCATIONS: tuple[Path, ...] = (
    Path(".claude-plugin") / "plugin.json",
    Path("plugin.json"),
)


def resolve_manifest(root: str | Path) -> Path | None:
    """Return the first existing manifest under ``root``, or ``None``.

    Walks ``MANIFEST_LOCATIONS`` in order and returns the path of the first entry
    that is an existing file, preserving ``.claude-plugin/plugin.json`` precedence
    over a root ``plugin.json``. Returns ``None`` when neither exists.
    """
    base = Path(root)
    return next((base / loc for loc in MANIFEST_LOCATIONS if (base / loc).is_file()), None)
