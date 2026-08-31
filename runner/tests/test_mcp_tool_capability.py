"""Runtime evidence for deciding whether a session can perform a write action."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from curie_runner.mcp_tool_capability import probe_mcp_tool_capability

_SERVER = Path(__file__).parent / "fixtures" / "mcp_tool_capability_server.py"


def _bundle(tmp_path: Path, *, mode: str) -> Path:
    root = tmp_path / mode
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "acme-bot"}), encoding="utf-8"
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "operations": {
                        "command": sys.executable,
                        "args": [str(_SERVER)],
                        "env": {"CURIE_TEST_TOOL_MODE": mode},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _probe(root: Path):
    return anyio.run(probe_mcp_tool_capability, root, {})


def test_all_tools_explicitly_read_only_proves_no_write_capability(tmp_path: Path) -> None:
    # Provider contract: MCP ToolAnnotations.readOnlyHint=true means the tool
    # does not modify its environment; absence defaults false. The same section
    # says annotations are untrusted hints, so this result only removes Curie's
    # model-invoked generic pager -- it never authorizes a tool or bypasses an
    # explicit gate.
    # https://modelcontextprotocol.io/specification/2025-11-25/schema#toolannotations
    result = _probe(_bundle(tmp_path, mode="read-only"))

    assert result.complete
    assert not result.has_potential_write_tool
    assert result.tool_count == 1


def test_explicit_write_tool_keeps_write_capability(tmp_path: Path) -> None:
    result = _probe(_bundle(tmp_path, mode="write"))

    assert result.complete
    assert result.has_potential_write_tool


def test_missing_read_only_hint_is_conservatively_write_capable(tmp_path: Path) -> None:
    result = _probe(_bundle(tmp_path, mode="unknown"))

    assert result.complete
    assert result.has_potential_write_tool


def test_unreachable_server_cannot_be_mistaken_for_read_only(tmp_path: Path) -> None:
    root = _bundle(tmp_path, mode="read-only")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "operations": {
                        "command": str(root / "missing-server"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = _probe(root)

    assert not result.complete
    assert result.has_potential_write_tool
    assert result.failures == ("operations",)


def test_no_mcp_servers_is_a_complete_empty_surface(tmp_path: Path) -> None:
    root = _bundle(tmp_path, mode="read-only")
    (root / ".mcp.json").unlink()

    result = _probe(root)

    assert result.complete
    assert not result.has_potential_write_tool
    assert result.tool_count == 0
