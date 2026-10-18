"""Skill framework tests — the contract, the phase gate, and the three
example skills.

All doubles are local: a scripted executor (records every call, answers
from a script), a fake profile (workspace entries by text), a fake
runner (records the job spec, answers with one result), and the shared
FakeEventStore. No network, no database — the skills are deterministic,
so their tests can be too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeEventStore

from aether.config import ProactiveConfig
from aether.llm.types import ToolCall
from aether.memory.events import Event
from aether.proactive.store import Opportunity, ProactiveTask
from aether.proactive.worker import READ_TOOLS, read_only
from aether.skills import FollowupsSkill, MediaSkill, ResearchSkill, build_skills
from aether.skills.base import held_approval
from aether.skills.research import _week
from aether.web import _NOT_CONNECTED
from aether.workbench.base import (
    BACKGROUND,
    LABEL,
    LATENCY_CLASS,
    ORDINARY,
    SENSITIVITY,
    TASK_ID,
    JobResult,
)


def _event(
    event_id: int,
    *,
    kind: str = "chat_message",
    payload: dict | None = None,
    source: str = "telegram",
    memorable: bool = True,
) -> Event:
    return Event(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=datetime.now(UTC),
        payload=dict(payload or {}),
        salience_score=1.0,
        memorable=memorable,
        meta={},
    )


def _task(detail: dict[str, Any], *, skill: str = "followups", label: str = "x") -> ProactiveTask:
    now = datetime.now(UTC)
    return ProactiveTask(
        id=1,
        skill=skill,
        label=label,
        status="pending",
        opportunity_key="k",
        opportunity=dict(detail),
        plan=None,
        result=None,
        feedback=None,
        feedback_note=None,
        provider=None,
        day="2026-10-18",
        created_at=now,
        updated_at=now,
    )


class ScriptedExecutor:
    """The executor double: records every call, answers by tool name."""

    def __init__(self, replies: dict[str, str] | None = None, default: str = "done") -> None:
        self.calls: list[ToolCall] = []
        self.replies = dict(replies or {})
        self.default = default

    async def __call__(self, call: ToolCall) -> str:
        self.calls.append(call)
        return self.replies.get(call.name, self.default)


class FakeProfile:
    """The workspace slice the skills read: confirmed entries by text."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.entries = [SimpleNamespace(text=t) for t in (texts or [])]

    def profile(self) -> list[Any]:
        return self.entries


class FakeRunner:
    """The workbench double: records specs, answers with one result."""

    def __init__(self, result: JobResult | None = None) -> None:
        self.result = result or JobResult(
            exit_ok=True, stdout="a 42-word document", stderr="", duration=30.0, provider="fake"
        )
        self.specs: list[Any] = []

    async def __call__(self, spec) -> JobResult:
        self.specs.append(spec)
        return self.result


# ---------------------------------------------------------------------------
# the phase gate — code, not a prompt line
# ---------------------------------------------------------------------------


async def test_read_only_refuses_writes_before_the_executor_is_asked() -> None:
    underlying = ScriptedExecutor()
    guarded = read_only(underlying)

    write = ToolCall(id="w", name="workspace_remember", arguments={"text": "n", "kind": "fact"})
    reply = await guarded(write)
    assert reply.startswith("not run — planning only looks things up")
    assert underlying.calls == []  # refused before the policy, before anything

    read = ToolCall(id="r", name="memory_search", arguments={"query": "promises"})
    assert await guarded(read) == "done"
    assert [c.name for c in underlying.calls] == ["memory_search"]


def test_the_read_vocabulary_is_the_research_and_memory_pair() -> None:
    assert {
        "memory_search",
        "workspace_read",
        "workspace_search",
        "web_fetch",
        "web_search",
    } == READ_TOOLS


# ---------------------------------------------------------------------------
# followups — promises found in memory, drafted for a reply
# ---------------------------------------------------------------------------


async def test_followups_detects_promises_keyed_by_event() -> None:
    events = FakeEventStore(
        {
            1: _event(1, payload={"text": "I'll get back to you on the deck"}),
            2: _event(2, payload={"text": "the meeting ran long"}),
            3: _event(3, payload={"text": "remind me to call the dentist"}),
        }
    )
    found = await FollowupsSkill().detect(FakeProfile(), events, None)
    assert [o.key for o in found] == ["followup:3", "followup:1"]  # newest first
    assert all(o.skill == "followups" for o in found)
    assert found[1].detail["text"].endswith("the deck")


async def test_followups_skips_plain_and_unmemorable_events() -> None:
    events = FakeEventStore(
        {
            1: _event(1, payload={"text": "I'll follow up"}, memorable=False),
            2: _event(2, payload={"text": "no promise here"}),
        }
    )
    assert await FollowupsSkill().detect(FakeProfile(), events, None) == []
    # and no event store at all means nothing to look at
    assert await FollowupsSkill().detect(None, None, None) == []


async def test_followups_run_sends_the_reminder_to_chat_when_no_send_is_known() -> None:
    executor = ScriptedExecutor()
    task = _task({"event_id": 7, "text": "I'll send the deck", "source": "telegram", "address": ""})
    outcome = await FollowupsSkill().run(task, None, executor)
    assert outcome.summary == "the reminder is in your chat"
    assert outcome.refused is False
    (call,) = executor.calls
    assert call.name == "send_chat_message"
    assert "I'll send the deck" in call.arguments["text"]


async def test_followups_run_parks_an_external_reply_for_approval() -> None:
    executor = ScriptedExecutor(
        {"composio__GMAIL_SEND_EMAIL": "held for approval (#9) — the user has been asked"}
    )
    task = _task(
        {
            "event_id": 7,
            "text": "I'll send the deck",
            "source": "gmail",
            "address": "mike@example.com",
        }
    )
    outcome = await FollowupsSkill().run(task, None, executor)
    assert outcome.summary == "I drafted the follow-up reply and it's waiting for your OK"
    assert outcome.refused is False  # parked is asked, not refused
    assert outcome.approval_id == 9  # which staged action the Approve tap carries out
    (call,) = executor.calls
    assert call.name == "composio__GMAIL_SEND_EMAIL"
    assert call.arguments["recipient"] == "mike@example.com"


def test_held_approval_reads_the_parked_id_from_a_reply() -> None:
    assert held_approval("held for approval (#9) — the user has been asked") == 9
    assert held_approval("denied by policy: that action is turned off") is None
    assert held_approval("the reminder is in your chat") is None


async def test_followups_run_reports_a_denial_as_refused() -> None:
    executor = ScriptedExecutor(default="denied by policy: that action is turned off")
    task = _task({"event_id": 7, "text": "I'll send the deck", "source": "telegram", "address": ""})
    outcome = await FollowupsSkill().run(task, None, executor)
    assert outcome.refused is True
    assert outcome.summary.startswith("denied by policy")


async def test_followups_run_without_an_executor_fails_honestly() -> None:
    task = _task({"event_id": 7, "text": "I'll send the deck"})
    with pytest.raises(RuntimeError):
        await FollowupsSkill().run(task, None, None)


async def test_followups_revalidate_reads_the_event_back() -> None:
    events = FakeEventStore({5: _event(5, payload={"text": "I'll follow up"})})
    opp = Opportunity(key="followup:5", skill="followups", label="x", detail={"event_id": 5})
    assert await FollowupsSkill().revalidate(opp, None, events) is True
    opp = Opportunity(key="followup:9", skill="followups", label="x", detail={"event_id": 9})
    assert await FollowupsSkill().revalidate(opp, None, events) is False
    assert await FollowupsSkill().revalidate(opp, None, None) is False


# ---------------------------------------------------------------------------
# research — a profile topic, looked up and kept, once a week
# ---------------------------------------------------------------------------


async def test_research_detects_profile_topics_bucketed_by_week() -> None:
    found = await ResearchSkill().detect(FakeProfile(["vertical farming", ""]), None, None)
    assert [o.key for o in found] == [f"research:vertical farming:{_week()}"]
    assert found[0].detail["topic"] == "vertical farming"
    assert found[0].skill == "research"
    assert await ResearchSkill().detect(None, None, None) == []


async def test_research_run_searches_and_keeps_the_digest() -> None:
    executor = ScriptedExecutor(
        {
            "web_search": "Vertical farming guide — https://example.com/guide\nGrows in stacks.",
            "workspace_remember": "Saved to the workspace as entry e1.",
        }
    )
    task = _task({"topic": "vertical farming"}, skill="research")
    outcome = await ResearchSkill().run(task, None, executor)
    assert outcome.summary == "I looked up vertical farming and kept the notes in your workspace"
    search, note = executor.calls
    assert search.name == "web_search"
    assert search.arguments["query"] == "vertical farming"
    assert note.name == "workspace_remember"
    assert note.arguments["kind"] == "fact"
    assert note.arguments["category"] == "Research"
    assert "Grows in stacks" in note.arguments["text"]


async def test_research_run_never_saves_a_search_that_did_not_happen() -> None:
    executor = ScriptedExecutor({"web_search": _NOT_CONNECTED})
    task = _task({"topic": "vertical farming"}, skill="research")
    with pytest.raises(RuntimeError):
        await ResearchSkill().run(task, None, executor)
    # the transient refusals read the same way — nothing was looked at
    executor = ScriptedExecutor({"web_search": "the daily search cap is reached (25 a day)"})
    with pytest.raises(RuntimeError):
        await ResearchSkill().run(task, None, executor)


async def test_research_run_with_no_results_keeps_nothing() -> None:
    executor = ScriptedExecutor({"web_search": "No results for 'x'."})
    task = _task({"topic": "x"}, skill="research")
    outcome = await ResearchSkill().run(task, None, executor)
    assert outcome.summary == "I looked up x and found nothing to keep"
    assert [c.name for c in executor.calls] == ["web_search"]  # no note was attempted


async def test_research_revalidate_reads_the_profile_back() -> None:
    profile = FakeProfile(["vertical farming", "gothic cathedrals"])
    opp = Opportunity(key="r", skill="research", label="x", detail={"topic": "gothic cathedrals"})
    assert await ResearchSkill().revalidate(opp, profile, None) is True
    opp = Opportunity(key="r", skill="research", label="x", detail={"topic": "old news"})
    assert await ResearchSkill().revalidate(opp, profile, None) is False
    assert await ResearchSkill().revalidate(opp, None, None) is False


# ---------------------------------------------------------------------------
# media — a shared file without a note, described in the workbench
# ---------------------------------------------------------------------------


async def test_media_detects_uncaptioned_shared_documents() -> None:
    events = FakeEventStore(
        {
            1: _event(
                1,
                kind="shared_doc",
                payload={"filename": "deck.pdf", "text": "the deck's extracted words"},
            ),
            2: _event(
                2,
                kind="shared_doc",
                payload={"filename": "notes.txt", "text": "some words", "caption": "my notes"},
            ),
            3: _event(3, kind="shared_doc", payload={"filename": "scan.pdf", "text": ""}),
            4: _event(4, kind="shared_image", payload={"filename": "pic.jpg"}),
        }
    )
    found = await MediaSkill().detect(FakeProfile(), events, None)
    assert [o.key for o in found] == ["media:1"]
    assert found[0].detail["filename"] == "deck.pdf"
    assert await MediaSkill().detect(FakeProfile(), None, None) == []


async def test_media_run_dispatches_a_background_job_and_charges_its_time() -> None:
    runner = FakeRunner(
        JobResult(
            exit_ok=True, stdout="a 42-word document", stderr="", duration=30.5, provider="modal"
        )
    )
    task = _task(
        {"event_id": 1, "filename": "deck.pdf", "text": "the deck's extracted words"},
        skill="media",
        label="describe the shared file deck.pdf",
    )
    outcome = await MediaSkill().run(task, runner, None)
    assert outcome.summary == 'notes on "deck.pdf": a 42-word document'
    assert outcome.provider == "modal"
    assert outcome.sandbox_seconds == 30

    (spec,) = runner.specs
    assert spec.script_name == "describe.py"
    assert spec.meta[TASK_ID] == 1
    assert spec.meta[LABEL] == "describe the shared file deck.pdf"
    assert spec.meta[SENSITIVITY] == ORDINARY
    assert spec.meta[LATENCY_CLASS] == BACKGROUND
    assert spec.files["document.txt"] == "the deck's extracted words"
    assert "document.txt" in spec.files["describe.py"]


async def test_media_run_reports_a_job_that_failed_its_own_way() -> None:
    runner = FakeRunner(
        JobResult(exit_ok=False, stdout="", stderr="the tooling is missing", duration=12.0)
    )
    task = _task({"event_id": 1, "filename": "deck.pdf", "text": "words"}, skill="media")
    outcome = await MediaSkill().run(task, runner, None)
    assert outcome.failed is True
    assert outcome.refused is False
    assert "the tooling is missing" in outcome.summary
    assert outcome.sandbox_seconds == 12  # the time was truly spent


async def test_media_run_without_a_workbench_fails_honestly() -> None:
    task = _task({"event_id": 1, "filename": "deck.pdf", "text": "words"}, skill="media")
    with pytest.raises(RuntimeError):
        await MediaSkill().run(task, None, None)


async def test_media_revalidate_reads_the_file_back() -> None:
    events = FakeEventStore(
        {5: _event(5, kind="shared_doc", payload={"filename": "deck.pdf", "text": "w"})}
    )
    opp = Opportunity(key="media:5", skill="media", label="x", detail={"event_id": 5})
    assert await MediaSkill().revalidate(opp, None, events) is True
    opp = Opportunity(key="media:9", skill="media", label="x", detail={"event_id": 9})
    assert await MediaSkill().revalidate(opp, None, events) is False


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_the_registry_names_match_the_configured_skill_list() -> None:
    assert [s.name for s in build_skills()] == ProactiveConfig().skills
    # every registry skill declares its plain-words identity for the cards
    for skill in build_skills():
        assert skill.description
        assert skill.declared_actions
        assert skill.sensitivity in (ORDINARY, "sensitive")
