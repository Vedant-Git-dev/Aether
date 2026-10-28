"""Direct-provider tools in the flat namespace."""

from __future__ import annotations

import json

import pytest

from aether.connectors.registry import ToolRegistry
from aether.llm.types import ToolSpec


async def _echo(params: dict) -> dict:
    return {"ok": True, "text": params.get("text", "")}


def _spec(name: str = "demo__ping") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Ping the demo app",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        source="demo",
    )


def test_provider_tools_are_gated_until_mentioned() -> None:
    registry = ToolRegistry()
    registry.add_provider(_spec(), _echo, effect="read", source="demo", auth_mode="oauth")
    assert [spec.name for spec in registry.gated_specs("hello")] == []
    assert [spec.name for spec in registry.gated_specs("ping the demo app")] == ["demo__ping"]
    assert [spec.name for spec in registry.gated_specs("", unlocked={"demo__ping"})] == [
        "demo__ping"
    ]


@pytest.mark.asyncio
async def test_provider_tool_executes_and_reports_its_effect() -> None:
    registry = ToolRegistry()
    registry.add_provider(_spec(), _echo, effect="write", source="demo", auth_mode="oauth")
    tool = registry.get("demo__ping")
    assert tool is not None
    assert tool.kind == "provider"
    assert tool.effect == "write"
    assert tool.auth_mode == "oauth"
    result = json.loads(await registry.execute("demo__ping", {"text": "hi"}))
    assert result == {"ok": True, "text": "hi"}


def test_drop_app_only_removes_that_app() -> None:
    registry = ToolRegistry()
    registry.add_provider(_spec(), _echo, effect="read", source="demo", auth_mode="oauth")
    registry.add_provider(
        _spec("other__ping"), _echo, effect="read", source="other", auth_mode="oauth"
    )
    assert registry.drop_app("demo") == 1
    assert registry.get("demo__ping") is None
    assert registry.get("other__ping") is not None
    assert registry.provider_apps() == ["other"]


def test_gmail_tools_appear_for_the_words_people_actually_use() -> None:
    from aether.providers.google import GMAIL_APP

    registry = ToolRegistry()
    for tool in GMAIL_APP.tools:
        registry.add_provider(
            ToolSpec(
                name=f"gmail__{tool.key}",
                description=tool.description,
                input_schema=tool.input_schema,
                source="gmail",
            ),
            _echo,
            effect=tool.effect,
            source="gmail",
            auth_mode="oauth",
        )

    assert [s.name for s in registry.gated_specs("check my mail")] == ["gmail__list_messages"]
    assert "gmail__get_message" in [s.name for s in registry.gated_specs("read my email")]
    assert "gmail__send_email" in [s.name for s in registry.gated_specs("send an email")]


def test_calendar_tools_appear_for_calendar_words() -> None:
    from aether.providers.google import CALENDAR_APP

    registry = ToolRegistry()
    for tool in CALENDAR_APP.tools:
        registry.add_provider(
            ToolSpec(
                name=f"calendar__{tool.key}",
                description=tool.description,
                input_schema=tool.input_schema,
                source="calendar",
            ),
            _echo,
            effect=tool.effect,
            source="calendar",
            auth_mode="oauth",
        )

    names = [s.name for s in registry.gated_specs("check my calendar")]
    assert "calendar__list_events" in names
    assert "calendar__create_event" in names
    assert "calendar__create_event" in [s.name for s in registry.gated_specs("schedule a meeting")]
    assert [s.name for s in registry.gated_specs("am i free")] == ["calendar__freebusy"]


def test_suppressing_a_prefix_removes_the_hub_copy() -> None:
    class FakeHost:
        def tool_specs(self):
            return [
                ToolSpec(
                    name="composio__GMAIL_SEND_EMAIL",
                    description="Send mail through the hub",
                    source="composio",
                )
            ]

    registry = ToolRegistry()
    registry.attach_mcp(FakeHost())
    registry.sync_mcp_tools()
    assert registry.get("composio__GMAIL_SEND_EMAIL") is not None
    assert registry.suppress_mcp_prefix("composio__GMAIL_") == 1
    assert registry.get("composio__GMAIL_SEND_EMAIL") is None
    registry.sync_mcp_tools()
    assert registry.get("composio__GMAIL_SEND_EMAIL") is None
