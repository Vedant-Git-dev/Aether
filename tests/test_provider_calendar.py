"""Calendar handlers: event windows, writes, delete, and availability."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.google import (
    CALENDAR_EVENTS,
    CALENDAR_READ,
    GMAIL_COMPOSE,
    GMAIL_READ,
    GOOGLE_PROVIDER,
    create_event,
    delete_event,
    freebusy,
    list_events,
    update_event,
)
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import ProviderConnection
from aether.secret_env import EnvResolver


class FakeCtx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path.endswith("/events"):
            return {
                "items": [
                    {
                        "id": "evt-1",
                        "summary": "Standup",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-11-01T10:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-11-01T10:30:00Z", "timeZone": "UTC"},
                        "location": "Room 1",
                        "organizer": {"email": "me@example.com"},
                        "htmlLink": "https://calendar.example/evt-1",
                    }
                ],
                "nextPageToken": "next",
            }
        if method == "POST" and path.endswith("/events"):
            return {**kwargs["json"], "id": "evt-new", "htmlLink": "https://calendar.example/new"}
        if method == "PATCH":
            return {**kwargs["json"], "id": "evt-1"}
        if path == "/freeBusy":
            return {
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-11-01T10:00:00Z",
                                "end": "2026-11-01T10:30:00Z",
                            }
                        ]
                    }
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    async def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        return httpx.Response(204)


@pytest.mark.asyncio
async def test_list_events_defaults_to_the_next_week_and_normalizes() -> None:
    ctx = FakeCtx()
    result = await list_events(ctx, {"query": "standup", "max_results": 5})
    assert result["successful"] is True
    event = result["data"]["events"][0]
    assert event["id"] == "evt-1"
    assert event["summary"] == "Standup"
    assert event["organizer"] == "me@example.com"
    assert result["data"]["next_page_token"] == "next"

    method, path, kwargs = ctx.calls[0]
    assert (method, path) == ("GET", "/calendars/primary/events")
    params = kwargs["params"]
    assert params["q"] == "standup"
    assert params["maxResults"] == 5
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    assert datetime.fromisoformat(params["timeMin"]) < datetime.fromisoformat(params["timeMax"])


@pytest.mark.asyncio
async def test_create_event_builds_a_timed_event_with_attendees() -> None:
    ctx = FakeCtx()
    result = await create_event(
        ctx,
        {
            "summary": "Planning",
            "start": "2026-11-01T10:00:00+00:00",
            "end": "2026-11-01T11:00:00+00:00",
            "time_zone": "UTC",
            "location": "Room 2",
            "attendees": ["ada@example.com"],
        },
    )
    assert result["successful"] is True
    assert result["data"]["event"]["id"] == "evt-new"
    body = ctx.calls[-1][2]["json"]
    assert body["summary"] == "Planning"
    assert body["start"] == {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"}
    assert body["attendees"] == [{"email": "ada@example.com"}]
    assert body["location"] == "Room 2"


@pytest.mark.asyncio
async def test_update_event_patches_only_the_given_fields() -> None:
    ctx = FakeCtx()
    result = await update_event(
        ctx,
        {
            "event_id": "evt 1",
            "summary": "Moved",
            "start": "2026-11-02T10:00:00+00:00",
            "end": "2026-11-02T11:00:00+00:00",
        },
    )
    assert result["successful"] is True
    method, path, kwargs = ctx.calls[-1]
    assert (method, path) == ("PATCH", "/calendars/primary/events/evt%201")
    assert kwargs["json"]["summary"] == "Moved"
    assert kwargs["json"]["start"]["dateTime"].startswith("2026-11-02T10:00")


@pytest.mark.asyncio
async def test_update_event_can_change_the_title_without_touching_the_time() -> None:
    ctx = FakeCtx()
    result = await update_event(ctx, {"event_id": "evt-1", "summary": "Renamed", "all_day": False})
    assert result["successful"] is True
    body = ctx.calls[-1][2]["json"]
    assert body == {"summary": "Renamed"}


@pytest.mark.asyncio
async def test_delete_event_uses_delete_and_returns_the_id() -> None:
    ctx = FakeCtx()
    result = await delete_event(ctx, {"event_id": "evt-1"})
    assert result["successful"] is True
    assert result["data"]["deleted"] == "evt-1"
    assert ctx.calls == [("DELETE", "/calendars/primary/events/evt-1", {})]


@pytest.mark.asyncio
async def test_freebusy_returns_the_busy_blocks() -> None:
    ctx = FakeCtx()
    result = await freebusy(
        ctx,
        {"time_min": "2026-11-01T00:00:00Z", "time_max": "2026-11-02T00:00:00Z"},
    )
    assert result["successful"] is True
    assert result["data"]["busy"][0]["start"] == "2026-11-01T10:00:00Z"
    method, path, kwargs = ctx.calls[-1]
    assert (method, path) == ("POST", "/freeBusy")
    assert kwargs["json"]["items"] == [{"id": "primary"}]


@pytest.mark.asyncio
async def test_missing_required_fields_are_errors_not_successes() -> None:
    assert (await create_event(FakeCtx(), {}))["successful"] is False
    assert (await update_event(FakeCtx(), {}))["successful"] is False
    assert (await delete_event(FakeCtx(), {}))["successful"] is False
    assert (await freebusy(FakeCtx(), {}))["successful"] is False


class _Store:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection

    async def list(self):
        return [self.connection]

    async def get(self, provider: str):
        return self.connection if provider == "google" else None

    async def get_sync(self, provider: str, app: str, key: str):
        return None

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        return None


class _OAuth:
    async def client_configured(self, provider) -> bool:
        return True


def _connection(scopes: tuple[str, ...]) -> ProviderConnection:
    return ProviderConnection(
        provider="google",
        auth_mode="oauth",
        app="calendar",
        access_token="access",
        scopes=" ".join(scopes),
        status="active",
    )


def _runtime(connection: ProviderConnection, registry: ToolRegistry, handler=None):
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={})))
    return ProviderRuntime(
        definitions=[GOOGLE_PROVIDER],
        store=_Store(connection),  # type: ignore[arg-type]
        oauth=_OAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=transport),
        registry=registry,
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver("/nonexistent"),
        config=AppConfig(),
        public_base_url="https://aether.test",
    )


@pytest.mark.asyncio
async def test_one_google_connection_registers_both_apps_tools() -> None:
    registry = ToolRegistry()
    runtime = _runtime(
        _connection((GMAIL_READ, GMAIL_COMPOSE, CALENDAR_READ, CALENDAR_EVENTS)),
        registry,
    )
    assert await runtime.register_tools() == 11
    assert registry.get("gmail__list_messages") is not None
    assert registry.get("calendar__list_events") is not None


@pytest.mark.asyncio
async def test_scopes_gate_each_app_without_reconnecting_the_other() -> None:
    gmail_only = ToolRegistry()
    gmail_runtime = _runtime(_connection((GMAIL_READ, GMAIL_COMPOSE)), gmail_only)
    assert await gmail_runtime.register_tools() == 6
    assert gmail_only.get("calendar__list_events") is None

    calendar_only = ToolRegistry()
    calendar_runtime = _runtime(_connection((CALENDAR_READ, CALENDAR_EVENTS)), calendar_only)
    assert await calendar_runtime.register_tools() == 5
    assert calendar_only.get("gmail__list_messages") is None


@pytest.mark.asyncio
async def test_calendar_tools_use_the_calendar_api_base() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"items": []})

    registry = ToolRegistry()
    runtime = _runtime(_connection((CALENDAR_READ, CALENDAR_EVENTS)), registry, handler)
    await runtime.register_tools()
    result = json.loads(await registry.execute("calendar__list_events", {"max_results": 1}))
    assert result["successful"] is True
    assert seen and seen[0].startswith(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?"
    )
