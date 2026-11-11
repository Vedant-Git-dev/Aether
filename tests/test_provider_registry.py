"""Direct-provider tools in the flat namespace."""

from __future__ import annotations

import json

import pytest

from aether.connectors.registry import ToolRegistry
from aether.llm.types import WireSpec


async def _echo(params: dict) -> dict:
    return {"ok": True, "text": params.get("text", "")}


def _spec(name: str = "demo__ping") -> WireSpec:
    return WireSpec(
        name=name,
        description="Ping the demo app",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        source="demo",
    )


def test_provider_tools_are_visible_but_not_core() -> None:
    registry = ToolRegistry()
    registry.add_provider(_spec(), _echo, effect="read", source="demo", auth_mode="oauth")
    assert registry.core_specs() == []
    assert [spec.wire_name for spec in registry.visible()] == ["demo__ping"]
    assert [spec.wire_name for spec in registry.search("ping demo")] == ["demo__ping"]
    loaded: set[str] = set()
    accepted, _ = registry.load(["demo__ping"], loaded)
    assert accepted == ["demo.ping"]
    loaded.update(accepted)
    assert [spec.name for spec in registry.schemas(loaded)] == ["demo__ping"]


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


def test_gmail_tools_become_the_email_capability() -> None:
    from aether.providers.google import GMAIL_APP

    registry = ToolRegistry()
    for tool in GMAIL_APP.tools:
        registry.add_provider(
            WireSpec(
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

    # the model-facing capability is email; gmail stays the backend and the
    # legacy wire name still resolves
    assert registry.get("gmail__list_messages").spec.id == "email.list_messages"
    assert registry.get("gmail__list_messages").spec.wire_name == "email__list_messages"
    assert registry.get("gmail__list_messages").spec.backends == ("gmail",)
    assert [s.wire_name for s in registry.search("check my mail")] == ["email__list_messages"]
    assert "email__get_message" in [s.wire_name for s in registry.search("read my email")]
    assert "email__send_email" in [s.wire_name for s in registry.search("send an email")]
    # a stored approval or old rule can still name the gmail form
    loaded: set[str] = set()
    accepted, _ = registry.load(["gmail__send_email"], loaded)
    assert accepted == ["email.send_email"]


def test_calendar_tools_appear_for_calendar_words() -> None:
    from aether.providers.google import CALENDAR_APP

    registry = ToolRegistry()
    for tool in CALENDAR_APP.tools:
        registry.add_provider(
            WireSpec(
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

    names = [s.wire_name for s in registry.search("check my calendar")]
    assert "calendar__list_events" in names
    assert "calendar__create_event" in names
    assert "calendar__create_event" in [s.wire_name for s in registry.search("schedule a meeting")]
    assert [s.wire_name for s in registry.search("am i free")] == ["calendar__freebusy"]


def test_github_tools_appear_for_github_words() -> None:
    from aether.providers.github import GITHUB_APP

    registry = ToolRegistry()
    for tool in GITHUB_APP.tools:
        registry.add_provider(
            WireSpec(
                name=f"github__{tool.key}",
                description=tool.description,
                input_schema=tool.input_schema,
                source="github",
            ),
            _echo,
            effect=tool.effect,
            source="github",
            auth_mode="oauth",
        )

    names = [s.wire_name for s in registry.search("check my github issues")]
    assert "github__list_issues" in names
    assert "github__get_issue" in names
    assert "github__create_issue" in [s.wire_name for s in registry.search("open an issue")]
    assert "github__list_notifications" in [
        s.wire_name for s in registry.search("github notifications")
    ]
    assert "github__search" in [s.wire_name for s in registry.search("search github")]
