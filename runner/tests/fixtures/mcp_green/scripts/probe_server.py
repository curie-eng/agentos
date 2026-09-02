"""Tiny stdio MCP server for the mcp_green fixture: exposes exactly one tool.

Copied/adapted from the plan's verified reference server; used by the real
loader (Claude Code CLI --plugin-dir) to prove that a well-formed inline
mcpServers declaration actually spawns and registers a tool.
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mcp-green-probe")


@mcp.tool()
def word_count(text: str) -> int:
    """Count whitespace-separated words."""
    return len(text.split())


if __name__ == "__main__":
    mcp.run()
