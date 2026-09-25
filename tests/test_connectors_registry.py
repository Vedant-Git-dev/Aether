"""ToolRegistry tests — flat namespace, dispatch, shadowing. All fakes."""

from __future__ import annotations

import pytest

from aether.connectors.base import UnknownToolError
from aether.connectors.registry import RegisteredTool, ToolRegistry
from aether.llm.types import ToolSpec


class FakeHost:
    """MCPHost double: canned specs, recorded calls."""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self.specs_list = specs
        self.calls: list[tuple[str, dict]] = []

    def tool_specs(self) -> list[ToolSpec]:
        return self.specs_list

    async def call(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return f"mcp:{name}"


def _native(name: str, source: str = "native") -> ToolSpec:
    return ToolSpec(name=name, description=f"native {name}", source=source)


async def _echo_handler(params: dict) -> dict:
    return {"echo": params.get("text", "")}


async def test_native_tools_execute_and_stringify() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("memory_search"), _echo_handler)

    spec = registry.specs()[0]
    assert spec.source == "native"  # the default
    assert await registry.execute("memory_search", {"text": "hi"}) == '{"echo": "hi"}'


async def test_native_string_results_pass_through() -> None:
    async def plain(params: dict) -> str:
        return "kept as-is"

    registry = ToolRegistry()
    registry.add_native(_native("plain"), plain)
    assert await registry.execute("plain", {}) == "kept as-is"


async def test_duplicate_native_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("t"), _echo_handler)
    with pytest.raises(ValueError):
        registry.add_native(_native("t"), _echo_handler)


async def test_mcp_tools_sync_into_the_namespace() -> None:
    registry = ToolRegistry()
    host = FakeHost([ToolSpec(name="mail__list_unread", description="unread mail", source="mail")])
    registry.attach_mcp(host)
    assert registry.sync_mcp_tools() == 1
    assert registry.sync_mcp_tools() == 0  # idempotent

    spec = registry.get("mail__list_unread")
    assert spec is not None and spec.kind == "mcp"
    assert registry.specs()[0].source == "mail"

    assert await registry.execute("mail__list_unread", {"limit": 2}) == "mcp:mail__list_unread"
    assert host.calls == [("mail__list_unread", {"limit": 2})]


async def test_native_tools_shadow_same_named_mcp_tools() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("mail__list_unread"), _echo_handler)
    host = FakeHost([ToolSpec(name="mail__list_unread", description="mcp one", source="mail")])
    registry.attach_mcp(host)
    registry.sync_mcp_tools()

    assert await registry.execute("mail__list_unread", {"text": "x"}) == '{"echo": "x"}'
    assert host.calls == []  # the native tool won


async def test_unknown_tools_do_not_execute() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError):
        await registry.execute("ghost", {})


async def test_mcp_tool_without_host_is_an_error() -> None:
    # defensive branch: an mcp entry can only exist via sync_mcp_tools, which
    # requires a host — but if one ever routes with no host attached it
    # must fail loudly, not silently do nothing
    registry = ToolRegistry()
    registry.add_native(_native("t"), _echo_handler)
    registry._tools["srv__x"] = RegisteredTool(
        ToolSpec(name="srv__x", description="", source="srv"), "mcp"
    )
    with pytest.raises(UnknownToolError):
        await registry.execute("srv__x", {})
