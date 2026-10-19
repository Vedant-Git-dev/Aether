"""The /status command and the cross-turn park dedup — one asks the
loop to say its own state plainly (resolved models, saved changes,
waiting approvals, the queue, an armed restart — the truth the running
process serves with, not raw config knobs where "base" reads as
nothing), the other keeps one request to one question in front of the
user across turns, the way the in-turn trace dedup already does inside
a single one. Both are deterministic end to end: no ingest, no model
woken for a command."""

from __future__ import annotations

from fakes import FakeConfigManager, FakeProvider
from test_agent_loop import LoopKit, _msg

import aether.restart
from aether.llm.types import ToolCall


async def test_status_names_the_resolved_models() -> None:
    provider = FakeProvider([])  # any LLM call in a command test is a bug
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("/status"))
    await kit.loop._tick()

    assert kit.connector.sent == [
        "Right now:\n"
        "- main model: fake-model\n"
        "- reading images: fake-model\n"
        "- listening to audio: fake-model\n"
        "- watching video: fake-model\n"
        "- no approvals waiting\n"
        "- nothing queued"
    ]
    assert provider.calls == []
    assert kit.events.ingested == []


async def test_status_says_nothing_set_when_no_model_answers() -> None:
    # an unwired loop must never guess a sense it doesn't have — "base"
    # and empty knobs both read as nothing here, resolved from the truth
    kit = LoopKit(None)
    kit.loop.submit_message(_msg("/status"))
    await kit.loop._tick()

    assert kit.connector.sent == [
        "Right now:\n"
        "- main model: nothing set\n"
        "- reading images: nothing set\n"
        "- listening to audio: nothing set\n"
        "- watching video: nothing set\n"
        "- no approvals waiting\n"
        "- nothing queued"
    ]


async def test_status_shows_saved_changes_and_waiting_approvals() -> None:
    manager = FakeConfigManager(
        effective_payload={"sections": {}, "overrides": [{"path": "llm.image_model"}]}
    )
    kit = LoopKit(FakeProvider([]), config_manager=manager)
    kit.add_tool("mail__send_message")
    # one call parked and still waiting — the status must count it
    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    assert len(kit.approvals.created) == 1

    kit.loop.submit_message(_msg("/status"))
    await kit.loop._tick()

    said = kit.connector.sent[0]
    assert "- changed from config.yaml: llm.image_model" in said
    assert "- 1 approval(s) waiting for you" in said


async def test_status_names_the_restart_window(monkeypatch) -> None:
    # the 5-second drain between a save and the fresh boot is the window
    # where new settings are stored but old providers still serve —
    # /status says so instead of leaving it mysterious
    monkeypatch.setattr(aether.restart, "_pending", True)
    kit = LoopKit(FakeProvider([]))
    kit.loop.submit_message(_msg("/status"))
    await kit.loop._tick()

    assert "- a restart is coming to load saved settings" in kit.connector.sent[0]


async def test_the_same_call_parks_once_across_turns() -> None:
    """One request means one question in front of the user: proposing the
    exact parked call again — a later turn, a nagging retry — gets the
    waiting row's answer, not a second approval that a later approve
    would send twice."""
    kit = LoopKit(None)
    kit.add_tool("mail__send_message")
    trace: list[dict] = []

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    assert len(kit.approvals.created) == 1

    result = await kit.loop._execute(
        ToolCall(id="t2", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"}),
        trace=trace,
    )

    waiting = kit.approvals.created[0]
    assert len(kit.approvals.created) == 1  # still one row
    assert f"already waiting for the user (#{waiting.id})" in result.content
    assert trace[-1]["approval_id"] == waiting.id
    # no second question was presented
    assert [a[0] for a in kit.connector.approvals_presented] == [waiting.id]
