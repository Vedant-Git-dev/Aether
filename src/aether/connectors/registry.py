"""The flat tool namespace the agent sees and executes through.

Native tools register with an async handler; MCP tools are discovered from
the host and routed back through it. One namespace, one dispatch point —
the agent loop passes every executed name through authz first, then hands
it here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..llm.types import ToolSpec
from .base import UnknownToolError
from .mcp_host import MCPHost

log = logging.getLogger("aether.connectors.registry")

NativeHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    kind: Literal["native", "mcp"]
    handler: NativeHandler | None = None


class ToolRegistry:
    """Everything the model can call, under one flat set of names."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._mcp_host: MCPHost | None = None

    # -- registration ----------------------------------------------------------

    def add_native(self, spec: ToolSpec, handler: NativeHandler) -> None:
        existing = self._tools.get(spec.name)
        if existing is not None and existing.kind == "native":
            raise ValueError(f"native tool already registered: {spec.name}")
        if existing is not None:
            log.warning("mcp tool %s is shadowed by a native tool of the same name", spec.name)
            return
        self._tools[spec.name] = RegisteredTool(spec, "native", handler)

    def attach_mcp(self, host: MCPHost) -> None:
        self._mcp_host = host

    def sync_mcp_tools(self) -> int:
        """Pull the host's current tool list into the namespace. Idempotent —
        safe to call repeatedly (e.g. whenever a server (re)connects)."""
        if self._mcp_host is None:
            return 0
        added = 0
        for spec in self._mcp_host.tool_specs():
            existing = self._tools.get(spec.name)
            if existing is not None:
                if existing.kind == "mcp":
                    self._tools[spec.name] = RegisteredTool(spec, "mcp")
                else:
                    log.warning(
                        "mcp tool %s is shadowed by a native tool of the same name",
                        spec.name,
                    )
                continue
            self._tools[spec.name] = RegisteredTool(spec, "mcp")
            added += 1
        return added

    # -- use ----------------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool: {name}")
        if tool.kind == "native":
            assert tool.handler is not None
            return _stringify(await tool.handler(params or {}))
        if self._mcp_host is None:
            raise UnknownToolError(f"mcp tool {name} has no host to route through")
        return await self._mcp_host.call(name, params or {})


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)
