"""Offline end-to-end of the #245 permission gate through the real boot path.

This drives the real boot wiring (``build_runner(..., fake_model=True)``) so the
approval gate is exercised together with config parsing, the session loop, and
NDJSON streaming -- not just its unit seam. The fake model replays a scripted Bash
tool call (``default_turn``); the gate must intercept it exactly as the SDK would,
with zero credential and zero network.

It fails against the pre-#413 fake factory, which builds a bare
``FakeModelSession()`` that ignores ``can_use_tool``: the gated turn ends ``done``
(not ``awaiting-approval``).

Bundle PreToolUse command hooks (#272) are deliberately NOT exercised in fake mode
-- they shell out, which would break the fake's offline no-op guarantee -- so their
bundle -> ``load_bundle_hooks`` -> deny wiring is covered offline in ``test_hooks``.
"""

from __future__ import annotations

import json

import anyio
from aci_protocol import Event
from curie_runner.__main__ import build_runner
from curie_runner.approval import PUBLISH_TOOL_NAME
from curie_runner.config import RunnerConfig

# A budget high enough that default_turn's 8 output tokens never trip the halt
# (a halt would outrank a pending approval and mask the gate under test).
_BUDGET = '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'


def _base_env(plugin_dir: str) -> dict[str, str]:
    return {
        "CURIE_PLUGIN_DIR": plugin_dir,
        "CURIE_SESSION_ID": "s-e2e",
        "CURIE_SANDBOX_ID": "b-e2e",
        "CURIE_BUDGET": _BUDGET,
    }


def _write_manifest(tmp_path, manifest: dict) -> str:
    """Write a minimal bundle manifest and return the plugin dir path."""
    plugin = tmp_path / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps(manifest))
    return str(tmp_path)


async def _drain(runner, text: str) -> list[dict[str, object]]:
    lines = [
        line
        async for line in runner.run_turn(
            Event(type="message", text=text, user="U", ts="1")
        )
    ]
    return [json.loads(line) for line in lines]


def test_gate_e2e_ends_awaiting_approval_through_build_runner(tmp_path) -> None:
    # A bundle-declared/env-configured approval-required tool (Bash) must end the
    # turn awaiting-approval on a fake runner built by the real boot path.
    plugin_dir = _write_manifest(tmp_path, {"name": "gated"})
    env = {**_base_env(plugin_dir), "CURIE_APPROVAL_REQUIRED_TOOLS": "Bash"}
    runner = build_runner(RunnerConfig.from_env(env), fake_model=True)

    async def go() -> None:
        await runner.start()
        frames = await _drain(runner, "run some bash")
        final = frames[-1]
        assert final["type"] == "final"
        assert final["status"] == "awaiting-approval"
        assert isinstance(final["approval_summary"], str)
        assert final["approval_summary"].startswith("Tool call awaiting approval: Bash")

    anyio.run(go)


def test_empty_policy_and_env_preserve_bypass_without_workspace_on_fake_boot_path(
    tmp_path,
) -> None:
    plugin_dir = _write_manifest(tmp_path, {"name": "publisher"})
    runner = build_runner(
        RunnerConfig.from_env(_base_env(plugin_dir)), fake_model=True
    )

    gate = runner._approval_gate  # noqa: SLF001 - boot wiring is the assertion
    assert gate is None


def test_empty_policy_and_env_preserve_bypass_without_workspace_on_real_boot_path(
    tmp_path,
) -> None:
    plugin_dir = _write_manifest(tmp_path, {"name": "publisher"})
    runner = build_runner(
        RunnerConfig.from_env(_base_env(plugin_dir)), fake_model=False
    )

    gate = runner._approval_gate  # noqa: SLF001 - boot wiring is the assertion
    assert gate is None


def test_managed_workspace_arms_mandatory_publish_on_fake_boot_path(tmp_path) -> None:
    plugin_dir = _write_manifest(tmp_path / "plugin", {"name": "publisher"})
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    runner = build_runner(
        RunnerConfig.from_env(_base_env(plugin_dir)),
        fake_model=True,
        workspace_path=workspace,
    )

    gate = runner._approval_gate  # noqa: SLF001 - boot wiring is the assertion
    assert gate is not None
    assert gate.required == frozenset({PUBLISH_TOOL_NAME})
