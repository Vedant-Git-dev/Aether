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


@server.tool()
def env(name: str) -> str:
    """One environment variable as this process sees it, or "" — the host
    tests use this to prove a $NAME in transport.env really got here."""
    import os

    return os.environ.get(name, "")


_grown = False


def _late() -> str:
    """A tool that only exists once grow() has been called."""
    return "i grew"


@server.tool()
def grow() -> str:
    """Register one more tool on this live server — the host tests use this
    to prove a live session's tool list is re-asked on a cadence, so an app
    approved after connect still reaches the vocabulary."""
    global _grown
    if not _grown:
        server.add_tool(_late, name="late")
        _grown = True
    return "grown"


if __name__ == "__main__":
    server.run()
