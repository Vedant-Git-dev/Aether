"""Native-tool tests — the six internal tools, exercised through the real
ToolRegistry so the wiring (spec name → handler) is what's under test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aether.agent.loop import CaptureRequestBox, SurfaceFanout
from aether.agent.tools import register_native_tools
from aether.connectors.registry import ToolRegistry
from aether.memory.events import Event
from fakes import FakeApprovals, FakeEntities, FakeEventStore, FakeScheduler, FakeSurfaceConnector


def _event(event_id: int) -> Event:
    return Event(
        id=event_id,
        source="telegram",
        kind="chat_message",
        occurred_at=datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc),
        payload={"text": "alice owes me a reply"},
        salience_score=7.0,
        memorable=True,
        meta={},
    )


class NativeKit:
    def __init__(self, events: FakeEventStore | None = None) -> None:
        self.registry = ToolRegistry()
        self.events = events or FakeEventStore()
        self.entities = FakeEntities()
        self.approvals = FakeApprovals()
        self.scheduler = FakeScheduler()
        self.connector = FakeSurfaceConnector()
        self.box = CaptureRequestBox()
        self.count = register_native_tools(
            registry=self.registry,
            events=self.events,
            entities=self.entities,
            approvals=self.approvals,
            scheduler=self.scheduler,
            surfaces=SurfaceFanout([self.connector]),
            capture_box=self.box,
        )

    async def run(self, name: str, params: dict) -> str:
        return await self.registry.execute(name, params)


async def test_six_native_tools_register() -> None:
    kit = NativeKit()
    assert kit.count == 6
    assert len(kit.registry) == 6
    names = {t.spec.name for t in (kit.registry.get(n) for n in (
        "memory_search", "note_entity", "get_pending_approvals",
        "schedule_action", "request_screen_capture", "send_chat_message",
    ))}
    assert names == {"memory_search", "note_entity", "get_pending_approvals",
                     "schedule_action", "request_screen_capture", "send_chat_message"}


async def test_memory_search_formats_hits_and_misses() -> None:
    kit = NativeKit(FakeEventStore(search_results=[_event(3)]))
    out = await kit.run("memory_search", {"query": "alice reply"})
    assert "[3] 2026-09-24 10:30 telegram/chat_message" in out
    assert "alice owes me a reply" in out

    empty = NativeKit()
    assert await empty.run("memory_search", {"query": "nothing"}) == "No matching memories."
    assert await empty.run("memory_search", {"query": "  "}) == "memory_search needs a query."


async def test_note_entity_resolves_then_records() -> None:
    kit = NativeKit()
    out = await kit.run("note_entity", {
        "handle": "alice@example.com", "note": "owes me a reply",
        "platform": "mail", "display_name": "Alice", "kind": "commitment",
    })
    assert "identity 1" in out
    assert kit.entities.resolved == [("mail", "alice@example.com")]
    identity_id, payload = kit.entities.notes[0]
    assert identity_id == 1
    assert payload["note"] == "owes me a reply"
    assert payload["kind"] == "commitment"

    assert await kit.run("note_entity", {"note": "no handle"}) == \
        "note_entity needs a handle and a note."


async def test_get_pending_approvals_lists_the_queue() -> None:
    kit = NativeKit()
    assert await kit.run("get_pending_approvals", {}) == "No pending approvals."

    await kit.approvals.create(
        tool_name="mail__send_message", params={"to": "a@b.c"}, note="risky",
    )
    out = await kit.run("get_pending_approvals", {})
    assert "#1 mail__send_message" in out
    assert "a@b.c" in out


async def test_schedule_action_persists_a_future_call() -> None:
    kit = NativeKit()
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    out = await kit.run("schedule_action", {
        "label": "evening summary",
        "run_at": when,
        "tool_name": "mail__send_message",
        "params": {"to": "me@example.com", "body": "summary"},
    })
    assert "authorization gate" in out
    created = kit.scheduler.created[0]
    assert created["label"] == "evening summary"
    assert created["payload"] == {
        "type": "tool", "tool": "mail__send_message",
        "params": {"to": "me@example.com", "body": "summary"},
    }
    assert created["run_at"].tzinfo is not None


async def test_schedule_action_rejects_bad_times() -> None:
    kit = NativeKit()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert await kit.run("schedule_action", {
        "label": "x", "run_at": past, "tool_name": "t",
    }) == "run_at must be in the future."
    assert await kit.run("schedule_action", {
        "label": "x", "run_at": "tomorrow please", "tool_name": "t",
    }) == "run_at 'tomorrow please' is not an ISO datetime (e.g. 2026-09-25T18:30:00+05:30)."
    assert kit.scheduler.created == []


async def test_request_screen_capture_round_trips_the_box() -> None:
    kit = NativeKit()
    out = await kit.run("request_screen_capture", {"reason": "check the deploy"})
    assert "companion" in out
    assert kit.box.take() == "check the deploy"
    assert kit.box.take() is None  # one capture per ask


async def test_send_chat_message_fans_out() -> None:
    kit = NativeKit()
    out = await kit.run("send_chat_message", {"text": "the deploy finished"})
    assert out == "Sent to the user's chat surfaces."
    assert kit.connector.sent == ["the deploy finished"]
    assert await kit.run("send_chat_message", {"text": "  "}) == \
        "send_chat_message needs text."
