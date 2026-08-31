"""Tiny stdio MCP server for the runner's tool-capability integration tests."""

from __future__ import annotations

import os

import anyio
from mcp import Tool
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, ToolAnnotations


async def main() -> None:
    server = Server("capability-fixture", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        mode = os.environ.get("CURIE_TEST_TOOL_MODE", "unknown")
        annotations = None
        if mode == "read-only":
            annotations = ToolAnnotations(readOnlyHint=True)
        elif mode == "write":
            annotations = ToolAnnotations(readOnlyHint=False)
        return [
            Tool(
                name="inspect_or_change",
                description="Test-only MCP tool.",
                inputSchema={"type": "object"},
                annotations=annotations,
            )
        ]

    @server.call_tool()
    async def call_tool(_name: str, _arguments: dict[str, object]) -> list[TextContent]:
        return [TextContent(type="text", text="ok")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
