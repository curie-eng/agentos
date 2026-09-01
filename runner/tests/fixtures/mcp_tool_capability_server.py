"""Tiny stdio MCP server for the runner's tool-capability integration tests."""

from __future__ import annotations

import os

import anyio
from mcp import Tool, types
from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, ToolAnnotations


async def list_tools(
    _context: ServerRequestContext[object],
    params: types.PaginatedRequestParams | None,
) -> ListToolsResult:
    """Serve one or two tools so the client exercises MCP pagination."""

    mode = os.environ.get("CURIE_TEST_TOOL_MODE", "unknown")
    annotations = None
    if mode in {"read-only", "paginated"}:
        annotations = ToolAnnotations(readOnlyHint=True)
    elif mode == "write":
        annotations = ToolAnnotations(readOnlyHint=False)
    if mode == "paginated" and (params is None or params.cursor is None):
        return ListToolsResult(
            tools=[
                Tool(
                    name="inspect_first_page",
                    description="Test-only MCP tool.",
                    inputSchema={"type": "object"},
                    annotations=annotations,
                )
            ],
            nextCursor="second-page",
        )
    return ListToolsResult(
        tools=[
            Tool(
                name="inspect_or_change",
                description="Test-only MCP tool.",
                inputSchema={"type": "object"},
                annotations=annotations,
            )
        ]
    )


async def call_tool(
    _context: ServerRequestContext[object], _params: types.CallToolRequestParams
) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="ok")])


async def main() -> None:
    server = Server(
        "capability-fixture",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(main)
