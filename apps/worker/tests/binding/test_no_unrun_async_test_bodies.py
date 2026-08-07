"""A nested ``async def`` inside a test that nobody ever runs is a silent pass.

The house pattern for an async test in this repo is a sync ``test_*`` that
defines a nested coroutine function and drives it with ``asyncio.run(go())``.
Omit that last line and the test still passes: no coroutine object is ever
created, so there is not even a ``RuntimeWarning`` to notice, and the assertions
inside are dead text. Replacing such a body with ``raise AssertionError`` keeps
it green.

This has now happened twice in one file (#1376, #1394). The second instance was
written in the same PR whose docstring recorded the first, which is the tell that
review attention is not the control here. Counting is not the control either:
``grep -c 'def test_'`` against ``grep -c 'asyncio.run(go())'`` balanced at 19
and 19 across the file that contained the defect, because one test mentioned the
call in its docstring while another was missing it. Only structure catches this.

The rule: a coroutine function defined as a DIRECT child of a ``test_*`` body,
whose name is never loaded anywhere in that test, is unreachable. Loading covers
every way it could legitimately be used -- ``asyncio.run(go())``,
``loop.run_until_complete(go())``, ``anyio.run(go)``, passing it to a helper --
so a test that drives its coroutine by any means at all is not flagged. A
docstring mentioning the call is text, not a load, and does not rescue it.

Lives under ``apps/worker/tests/binding`` for the same reason
``test_boot_env_single_declaration.py`` does: it is the closest lane already
collected by ``pyproject.toml``'s testpaths, so it runs in CI with no config
change. It is deliberately repo-wide despite the location, and it touches
nothing but the filesystem.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The repo root, four parents up from apps/worker/tests/binding/<this file>.
REPO_ROOT = Path(__file__).resolve().parents[4]

# Directories that are not ours to police.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", ".mypy_cache"}


def _test_files() -> list[Path]:
    """Every `test_*.py` in the repo, excluding vendored and build trees.

    Returns:
        Paths to the test modules this gate scans.
    """
    return [
        p
        for p in REPO_ROOT.rglob("test_*.py")
        if not any(part in SKIP_DIRS for part in p.parts)
    ]


def _unrun_coroutines(tree: ast.AST) -> list[tuple[str, str, int]]:
    """Find coroutine functions defined in a test body that nothing ever loads.

    Args:
        tree: the parsed module.

    Returns:
        `(test_name, coroutine_name, lineno)` for each unreachable coroutine.
    """
    findings: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        # Direct children only: a coroutine nested deeper belongs to whatever
        # defines it, and that enclosing scope is what would have to run it.
        nested = [c for c in node.body if isinstance(c, ast.AsyncFunctionDef)]
        if not nested:
            continue
        # ast.Load covers every legitimate use: the argument to asyncio.run, to
        # loop.run_until_complete, to anyio.run, or to any helper. A Store (the
        # def itself) is not a load, and neither is a docstring mention.
        loaded = {
            n.id
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        findings.extend(
            (node.name, c.name, c.lineno) for c in nested if c.name not in loaded
        )
    return findings


def test_no_test_defines_a_coroutine_it_never_runs() -> None:
    problems: list[str] = []
    for path in _test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is another gate's job
            continue
        rel = path.relative_to(REPO_ROOT)
        problems.extend(
            f"  {rel}:{lineno}  {test}() defines `{coro}` and never runs it"
            for test, coro, lineno in _unrun_coroutines(tree)
        )

    assert not problems, (
        "These tests define a coroutine and never drive it, so their assertions "
        "never execute and the test passes no matter what the code does. Add the "
        "`asyncio.run(...)` call, or delete the dead body:\n" + "\n".join(problems)
    )


def test_the_gate_catches_the_shape_it_is_written_for() -> None:
    """The self-test half: a gate that cannot fail is the defect it guards against.

    Pins both directions on synthetic sources, so the gate keeps discriminating
    even after every real instance is fixed and the scan above goes quiet.
    """
    unrun = ast.parse(
        "def test_x():\n"
        "    async def go():\n"
        "        assert False\n"
    )
    assert _unrun_coroutines(unrun) == [("test_x", "go", 2)]

    driven = ast.parse(
        "import asyncio\n"
        "def test_x():\n"
        "    async def go():\n"
        "        assert False\n"
        "    asyncio.run(go())\n"
    )
    assert _unrun_coroutines(driven) == []

    # A docstring naming the call is text, not a load.
    mentioned = ast.parse(
        "def test_x():\n"
        '    """This one is missing its asyncio.run(go())."""\n'
        "    async def go():\n"
        "        assert False\n"
    )
    assert len(_unrun_coroutines(mentioned)) == 1

    # Driven by something other than asyncio.run: not our business to flag.
    other_driver = ast.parse(
        "def test_x():\n"
        "    async def go():\n"
        "        assert False\n"
        "    anyio.run(go)\n"
    )
    assert _unrun_coroutines(other_driver) == []
