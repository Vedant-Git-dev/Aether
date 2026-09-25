"""Screen-vision tests — FakeProvider + FakeEventStore; no network, no DB."""

from __future__ import annotations

from aether.connectors.screenvision import ScreenVision
from aether.llm.types import Turn
from fakes import FakeEventStore, FakeProvider, FakeRegistry


def _vision(json_text: str) -> ScreenVision:
    return ScreenVision(
        FakeRegistry(FakeProvider([Turn(text=json_text)])),
        FakeEventStore(),
    )


async def test_record_describes_the_screen_and_stores_an_event() -> None:
    sv = _vision(
        '{"app": "git", "summary": "a merge conflict in main.py", '
        '"people": ["reviewer@example.com"], "commitments": ["respond to review by friday"], '
        '"actionables": []}'
    )
    result = await sv.record(b"\x89PNG-fake-bytes", "fix this before standup")
    assert result is not None and result.stored is True

    record = sv._events.ingested[0]
    assert record["source"] == "screen"
    assert record["kind"] == "screen_capture"
    payload = record["payload"]
    assert payload["app"] == "git"
    assert payload["commitments"] == ["respond to review by friday"]
    assert payload["user_note"] == "fix this before standup"
    assert "actionables" not in payload  # empty values dropped


async def test_record_without_a_vision_provider_returns_none() -> None:
    sv = ScreenVision(FakeRegistry(None), FakeEventStore())
    assert await sv.record(b"png") is None
    assert sv._events.ingested == []


async def test_record_with_a_blind_provider_returns_none() -> None:
    provider = FakeProvider([Turn(text="{}")])
    provider.supports_vision = False
    sv = ScreenVision(FakeRegistry(provider), FakeEventStore())
    assert await sv.record(b"png") is None
    assert sv._events.ingested == []


async def test_non_json_vision_turns_still_store_something() -> None:
    sv = _vision("The screen shows a broken CI pipeline, no JSON this time")
    result = await sv.record(b"png", note="look at this")
    assert result is not None and result.stored is True
    payload = sv._events.ingested[0]["payload"]
    assert payload["summary"].startswith("The screen shows")
    assert payload["user_note"] == "look at this"
