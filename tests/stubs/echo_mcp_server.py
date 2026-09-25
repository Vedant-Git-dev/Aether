"""A tiny MCP server (stdio) used by the mcp-host tests.

Run directly — it speaks MCP on stdin/stdout:

    python echo_mcp_server.py
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    server.run()
