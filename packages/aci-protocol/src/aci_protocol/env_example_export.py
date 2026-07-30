"""Regenerate the boot-env reference block in the repo's ``.env.example``.

``.env.example`` is the one place an operator looks to learn what the platform
reads from the environment, and it documented zero boot vars before #488 -- the
whole boot surface was discoverable only by grepping two lanes for string
literals. This module renders that surface from ``BootEnv`` into a delimited
block, so the documentation cannot drift from the model: ``check-contracts.sh``
regenerates it and fails on ``git diff --exit-code``.

Only the block between the markers is generated; everything else in the file is
hand-written and preserved verbatim. Run as
``python -m aci_protocol.env_example_export`` to rewrite it.
"""

from pathlib import Path

from .session import BootEnv, Producer

_BEGIN = "# --- BEGIN generated boot env (aci_protocol.env_example_export) ---"
_END = "# --- END generated boot env ---"

# What each producer is, in the operator's terms. Only ``operator`` names
# something a human sets; the rest are listed so the boot surface is greppable
# from one place rather than reverse-engineered from two lanes.
_PRODUCER_NOTES: dict[Producer, str] = {
    "worker": "the worker binding, per claim",
    "kernel": "the worker kernel's resume overlay",
    "substrate": "the chart / docker substrate",
    "operator": "you, via chart runner.extraEnv or docker -e",
}


def env_example_path() -> Path:
    """The repo-root ``.env.example`` this module owns a block of."""

    return Path(__file__).resolve().parents[4] / ".env.example"


def render_block() -> str:
    """Render the delimited boot-env block (no trailing newline).

    Keys come from ``BootEnv.env_keys()``, which is sorted, so regeneration is
    byte-identical and the drift gate cannot flap on field-declaration order.
    """

    width = max(len(key) for key in BootEnv.env_keys())
    lines = [
        _BEGIN,
        "# The worker-to-runner boot contract (#488, ADR-0049). GENERATED from",
        "# aci_protocol.session.BootEnv -- do not hand-edit; regenerate with",
        "# scripts/check-contracts.sh, which also fails the build on drift.",
        "#",
        "# These are NOT compose variables. The boot env is assembled per sandbox by",
        "# four producers and read by one consumer (the runner), so setting one here",
        "# does nothing on its own. They are documented in one place because the",
        "# alternative -- grepping the worker and runner lanes for string literals --",
        "# is what let this surface drift silently. Producers:",
        "#",
    ]
    for producer, note in _PRODUCER_NOTES.items():
        lines.append(f"#   {producer:<9} {note}")
    lines.append("#")
    for key in BootEnv.env_keys():
        producers = ", ".join(p for p in _PRODUCER_NOTES if key in BootEnv.env_keys(producer=p))
        lines.append(f"# {key:<{width}}  producers: {producers}")
    lines.append(_END)
    return "\n".join(lines)


def render_env_example(current: str) -> str:
    """Return ``current`` with the generated block replaced or appended."""

    block = render_block()
    start = current.find(_BEGIN)
    if start == -1:
        body = current if current.endswith("\n") else current + "\n"
        return f"{body}\n{block}\n"
    end = current.find(_END, start)
    if end == -1:
        raise ValueError(
            f"{env_example_path()} has a {_BEGIN!r} marker with no matching {_END!r}; "
            "restore the closing marker rather than letting the block swallow the file"
        )
    return current[:start] + block + current[end + len(_END) :]


def write_env_example() -> Path:
    """Rewrite the generated block in ``.env.example`` and return the path."""

    path = env_example_path()
    path.write_text(render_env_example(path.read_text(encoding="utf-8")), encoding="utf-8")
    return path


if __name__ == "__main__":
    written = write_env_example()
    print(f"wrote {written}")
