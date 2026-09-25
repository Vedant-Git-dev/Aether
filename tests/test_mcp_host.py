"""MCP host tests — a real mcp SDK stub server over stdio, no network, no DB.

The stub (tests/stubs/echo_mcp_server.py) is spawned as a subprocess with
the same interpreter, so these exercise the full client session: spawn,
initialize, list_tools, call_tool, and reconnect-after-failure.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from aether.config import MCPServerConfig, TransportConfig
from aether.connectors.base import ConnectorUnavailableError, UnknownToolError
from aether.connectors.mcp_host import MCPHost

STUB = Path(__file__).parent / "stubs" / "echo_mcp_server.py"


def _stub_config(name: str = "stub", **kwargs) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport=TransportConfig(type="stdio", command=sys.executable, args=[str(STUB)]),
        **kwargs,
    )


async def test_stub_server_lists_tools_and_calls_them() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}

        specs = {s.name: s for s in host.tool_specs()}
        assert set(specs) == {"stub__echo", "stub__add"}
        assert specs["stub__echo"].source == "stub"
        assert "text" in specs["stub__echo"].input_schema.get("properties", {})

        assert await host.call("stub__echo", {"text": "hello world"}) == "hello world"
        assert await host.call("stub__add", {"a": 2, "b": 3}) == "5"
    finally:
        await host.stop()


async def test_tool_level_errors_come_back_as_text() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        await host.wait_all_ready(timeout=30)
        # the server reports a bad tool as an error-marked result — that is
        # an answer for the agent to read, not a connection problem, so the
        # session must survive it untouched
        result = await host.call("stub__no_such_tool", {})
        assert result.startswith("error:")
        assert await host.call("stub__echo", {"text": "still here"}) == "still here"
    finally:
        await host.stop()


async def test_transport_failure_breaks_the_hold_and_reconnects() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        await host.wait_all_ready(timeout=30)
        conn = host.connections[0]
        real_session = conn._session

        class ExplodingSession:  # simulates the transport dying under us
            async def call_tool(self, tool, args):
                raise RuntimeError("transport is dead")

        conn._session = ExplodingSession()
        with pytest.raises(ConnectorUnavailableError):
            await host.call("stub__echo", {"text": "x"})
        # the failure broke the hold; the runner tears the session down and
        # reconnects with backoff. Waiting on the session object (not the
        # ready flag — it is still set until the teardown finishes) makes
        # this deterministic.
        fresh = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            session = conn._session
            if session is not None and not isinstance(session, ExplodingSession):
                fresh = session
                break
            await asyncio.sleep(0.1)
        assert fresh is not None, "the connection never came back after the transport failure"
        assert fresh is not real_session
        assert await host.call("stub__echo", {"text": "back again"}) == "back again"
    finally:
        await host.stop()


async def test_disabled_servers_never_spawn() -> None:
    host = MCPHost([_stub_config(enabled=False)])
    host.start()
    assert host.connections == []
    assert await host.wait_all_ready(timeout=1) == {}
    assert host.tool_specs() == []
    await host.stop()


async def test_dead_server_is_reported_not_raised() -> None:
    host = MCPHost([
        MCPServerConfig(
            name="broken",
            transport=TransportConfig(
                type="stdio", command="/nonexistent/aether-binary", args=[]
            ),
        )
    ])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=2) == {"broken": False}
    finally:
        await host.stop()


async def test_call_routing_errors() -> None:
    host = MCPHost([_stub_config()])
    with pytest.raises(UnknownToolError):
        await host.call("nosuch__tool", {})  # no server by that name
    with pytest.raises(UnknownToolError):
        await host.call("no_separator", {})  # not <server>__<tool>
