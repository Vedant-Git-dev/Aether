"""The MCP host: one long-lived client session per configured server.

Every server in config.yaml `mcp_servers` is connected at boot over stdio
or streamable HTTP, and each tool it exposes enters the flat namespace as
`<server>__<tool>`. Sessions are held open and reconnected with backoff on
failure — a dead or misconfigured server logs a warning and keeps retrying
but never takes the agent down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ..config import MCPServerConfig, PollTool
from ..llm.types import ToolSpec
from .base import ConnectorUnavailableError, UnknownToolError

log = logging.getLogger("aether.connectors.mcp")

DEFAULT_READY_TIMEOUT = 10.0
_MAX_BACKOFF = 30.0


class MCPServerConnection:
    """One server: session lifecycle, tool listing, and calls.

    The session lives inside a background task (`_run`): open the
    transport, initialize, list tools, signal ready, then park until either
    `stop()` or a failed call breaks the hold — at which point the loop
    reconnects with exponential backoff.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.name = config.name
        self.poll_tools: list[PollTool] = list(config.poll_tools)
        self._config = config
        self._ready = asyncio.Event()
        self._hold = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._session: Any = None
        self._tools: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        self._hold.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._session = None
        self._ready.clear()

    async def _run(self) -> None:
        backoff = 1.0
        try:
            while not self._stopping:
                self._hold.clear()
                try:
                    async with self._open_session() as session:
                        await session.initialize()
                        self._session = session
                        result = await session.list_tools()
                        self._tools = {tool.name: tool for tool in result.tools}
                        self._ready.set()
                        log.info("mcp server %s ready — %d tools", self.name, len(self._tools))
                        backoff = 1.0
                        await self._hold.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._stopping:
                        log.warning("mcp server %s connection problem: %s", self.name, exc)
                finally:
                    self._session = None
                    self._ready.clear()
                if self._stopping:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
        finally:
            self._session = None
            self._ready.clear()

    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[Any]:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        transport = self._config.transport
        if transport.type == "stdio":
            from mcp import StdioServerParameters

            params = StdioServerParameters(
                command=transport.command or "",
                args=list(transport.args),
                env=dict(transport.env) if transport.env else None,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    yield session
        else:
            from mcp.client.streamable_http import streamable_http_client

            http_client = None
            if transport.headers:
                import httpx2  # mcp's HTTP stack; ships with the mcp package

                http_client = httpx2.AsyncClient(headers=dict(transport.headers))
            try:
                async with streamable_http_client(
                    transport.url or "", http_client=http_client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        yield session
            finally:
                if http_client is not None:
                    await http_client.aclose()

    # -- readiness ------------------------------------------------------------

    async def wait_ready(self, timeout: float = DEFAULT_READY_TIMEOUT) -> bool:
        if self._task is None:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except TimeoutError:
            return False

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    # -- tools ----------------------------------------------------------------

    def tool_specs(self) -> list[ToolSpec]:
        """Current tools with their flat-namespace names. Empty until connected."""
        specs: list[ToolSpec] = []
        for tool in self._tools.values():
            schema = getattr(tool, "input_schema", None) or {
                "type": "object",
                "properties": {},
            }
            description = (getattr(tool, "description", "") or "").strip()
            specs.append(
                ToolSpec(
                    name=f"{self.name}__{tool.name}",
                    description=description or f"{self.name} tool {tool.name}",
                    input_schema=schema,
                    source=self.name,
                )
            )
        return specs

    async def call(self, tool: str, args: dict[str, Any]) -> str:
        if self._stopping or self._task is None:
            raise ConnectorUnavailableError(f"mcp server {self.name} is not running")
        if not await self.wait_ready():
            raise ConnectorUnavailableError(f"mcp server {self.name} is not connected")
        session = self._session
        if session is None:  # lost the race with a disconnect
            raise ConnectorUnavailableError(f"mcp server {self.name} is not connected")
        try:
            result = await session.call_tool(tool, args or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # the session is suspect — break the hold so the runner reconnects
            self._hold.set()
            raise ConnectorUnavailableError(
                f"mcp call {self.name}__{tool} failed: {exc}"
            ) from exc
        return _flatten(result)


def _flatten(result: Any) -> str:
    """A CallToolResult as plain text for the agent to read."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(str(getattr(block, "text", "")))
        elif kind == "image":
            parts.append("[image omitted]")
        else:
            parts.append(f"[{kind}]")
    if getattr(result, "is_error", False):
        return "error: " + ("\n".join(parts) or "the tool reported an error")
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    return json.dumps(structured) if structured else "(no content)"


class MCPHost:
    """All configured servers: start/stop, tool listing, and call routing."""

    def __init__(self, servers: list[MCPServerConfig]) -> None:
        self._connections = [MCPServerConnection(c) for c in servers if c.enabled]
        self._by_name = {c.name: c for c in self._connections}

    @property
    def connections(self) -> list[MCPServerConnection]:
        return list(self._connections)

    def start(self) -> None:
        for conn in self._connections:
            conn.start()

    async def stop(self) -> None:
        for conn in self._connections:
            await conn.stop()

    def tool_specs(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for conn in self._connections:
            specs.extend(conn.tool_specs())
        return specs

    async def call(self, qualified_name: str, args: dict[str, Any]) -> str:
        server, sep, tool = qualified_name.partition("__")
        if not sep:
            raise UnknownToolError(
                f"mcp tool names look like <server>__<tool>: got {qualified_name!r}"
            )
        conn = self._by_name.get(server)
        if conn is None:
            raise UnknownToolError(f"no configured mcp server named {server!r}")
        return await conn.call(tool, args)

    async def wait_all_ready(self, timeout: float = 30.0) -> dict[str, bool]:
        """Per-server readiness, all waited for concurrently."""
        if not self._connections:
            return {}

        async def one(conn: MCPServerConnection) -> tuple[str, bool]:
            return conn.name, await conn.wait_ready(timeout)

        results = await asyncio.gather(*(one(c) for c in self._connections))
        return dict(results)
