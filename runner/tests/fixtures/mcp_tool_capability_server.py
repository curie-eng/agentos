"""Tiny stdio MCP server for the runner's tool-capability integration tests."""

from __future__ import annotations

import os
from pathlib import Path

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
        if mode == "policy-catalog":
            return [
                Tool(
                    name="read_allowed",
                    description="Read a test value without changing external state.",
                    inputSchema={"type": "object"},
                    annotations=ToolAnnotations(readOnlyHint=True),
                ),
                Tool(
                    name="write_approval",
                    description="Write one test marker after approval.",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                    annotations=ToolAnnotations(readOnlyHint=False),
                ),
                Tool(
                    name="write_denied",
                    description="A test write forbidden by policy.",
                    inputSchema={"type": "object"},
                    annotations=ToolAnnotations(readOnlyHint=False),
                ),
                Tool(
                    name="write_unmatched",
                    description="A test write omitted from policy.",
                    inputSchema={"type": "object"},
                ),
            ]
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
    async def call_tool(name: str, _arguments: dict[str, object]) -> list[TextContent]:
        marker = os.environ.get("CURIE_TEST_CALL_MARKER")
        if marker:
            with Path(marker).open("a", encoding="utf-8") as output:
                output.write(f"{name}\n")
        return [TextContent(type="text", text=f"executed {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
