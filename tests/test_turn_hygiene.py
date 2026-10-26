"""Turn hygiene, unit: the typing pulse, the per-turn ceiling, and logs
that never carry what they shouldn't.

The observed symptoms this file pins: a message sent into minutes of
silence with nothing saying work was happening; a turn that could hang
forever with no plain word back; and held notes plus attachment filenames
written straight into the log file — "never log names of attachments or
any sensitive data" is a standing constraint, not a wish.

test_agent_loop.py owns the loop's general tests (and stays untouched);
test_messaging_units.py owns the connector seam (the typing method and
the oversize skip are pinned there). This file covers what the loop and
the attachment paths added.
"""

from __future__ import annotations

import asyncio
import logging

from fakes import FakeEventStore, FakeMediaStore, FakeProvider, FakeRegistry
from test_agent_loop import LoopKit, _event, _msg
from test_attachments import _image, _message

from aether.agent.tools import TIMEOUT_REPLY
from aether.config import AgentConfig, AppConfig
from aether.connectors.attachments import AttachmentProcessor, extract_doc_text
from aether.llm.types import Turn


class SlowProvider(FakeProvider):
    """The scripted fake, but each turn takes its time — long enough for
    the typing pulse to refresh while the turn runs."""

    def __init__(self, turns: list[Turn], delay: float) -> None:
        super().__init__(turns)
        self.delay = delay

    async def complete(self, system, messages, tools):
        await asyncio.sleep(self.delay)
        return await super().complete(system, messages, tools)


class HungProvider(FakeProvider):
    """A provider that never answers — the shape the ceiling exists for."""

    async def complete(self, system, messages, tools):
        await asyncio.sleep(30)


class TypingSurface:
    """The fanout double the pulse needs: a connector that records every
    typing fire and every send, speaking the same shapes as the loop's
    own fake surface."""

    def __init__(self) -> None:
        self.name = "typing"
        self.typing: int = 0
        self.sent: list[str] = []

    async def show_typing(self) -> None:
        self.typing += 1

    async def send_to_user(self, text: str) -> str:
        self.sent.append(text)
        return "m1"


def _wire_typing(kit: LoopKit) -> TypingSurface:
    """Put a typing-aware surface where the loop's fanout looks."""
    typing = TypingSurface()
    kit.loop._surfaces.connectors[0] = typing
    return typing


async def test_a_solicited_turn_shows_typing_until_it_ends(monkeypatch) -> None:
    """The minutes of silence after a message: the surface now says work
    is happening, keeps saying it for as long as the turn runs (Telegram's
    own marker fades after ~5s, so one fire is not enough), and stops the
    moment the turn ends."""
    monkeypatch.setattr("aether.agent.loop.TYPING_REFRESH_SECONDS", 0.02)
    provider = SlowProvider([Turn(text="done")], delay=0.07)
    kit = LoopKit(provider)
    typing = _wire_typing(kit)

    kit.loop.submit_message(_msg("draft the email"))
    await kit.loop._tick()

    assert typing.typing >= 2  # fired at once, refreshed while the turn ran
    assert typing.sent == ["done"]  # the turn itself still answered
    after = typing.typing
    await asyncio.sleep(0.05)  # past two more refresh intervals
    assert typing.typing == after  # the pulse died with the turn


async def test_an_unprompted_tick_never_pulses(monkeypatch) -> None:
    """Nobody is waiting on an answer when the agent talks to itself —
    an observation-only turn never starts the indicator."""
    monkeypatch.setattr("aether.agent.loop.TYPING_REFRESH_SECONDS", 0.02)
    kit = LoopKit(FakeProvider([Turn(text="saw the mail")]))
    typing = _wire_typing(kit)
    kit.events.events[1] = _event(1)  # an observation, no message anywhere

    await kit.loop._tick()

    assert typing.typing == 0
    assert typing.sent == ["saw the mail"]


async def test_a_hung_turn_ends_with_a_plain_reply_and_a_trace(caplog) -> None:
    """A provider that never answers used to hang the turn forever — now
    the ceiling ends it: plain words to the user, the fact in the trace,
    the tick carrying on, and the log line carrying the same short ref the
    trace keeps, so the two meet on one searchable token."""
    kit = LoopKit(
        HungProvider([]),
        config=AppConfig(agent=AgentConfig(turn_timeout_seconds=0.1)),
    )
    typing = _wire_typing(kit)

    with caplog.at_level(logging.WARNING, logger="aether.agent"):
        kit.loop.submit_message(_msg("what's in my inbox?"))
        await kit.loop._tick()  # returns: the ceiling is handled, not raised

    assert typing.sent == [TIMEOUT_REPLY]
    payload = kit.traces.created[-1]["payload"]
    assert "timed out" in payload["error"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert f"turn {payload['turn']} timed out" in logged


async def test_held_words_never_ride_the_log(caplog) -> None:
    """A held note is the message itself — it ships whole when the quiet
    window ends, and the log says only that words are being held, never
    which words."""
    kit = LoopKit(None, config=AppConfig(agent=AgentConfig(quiet_hours="00:00-23:59")))
    with caplog.at_level(logging.INFO, logger="aether.agent"):
        await kit.loop.note_to_user("the surprise party for Ravi is at 8")
        await kit.loop.present_result(1, "the report on Project Blue is ready")

    assert kit.loop._held == [
        "the surprise party for Ravi is at 8",
        "the report on Project Blue is ready",
    ]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "held for quiet hours" in logged
    assert "surprise" not in logged
    assert "Ravi" not in logged
    assert "Project Blue" not in logged


async def test_a_full_store_never_logs_the_files_name(caplog) -> None:
    """The store being full is a fact the log should say; which file it
    refused is the user's business, not the log's."""
    provider = FakeProvider([Turn(text='{"description": "a cat"}')])
    media = FakeMediaStore()
    media.full = True
    processor = AttachmentProcessor(FakeRegistry(provider), FakeEventStore(), media=media)

    with caplog.at_level(logging.WARNING, logger="aether.connectors.attachments"):
        results = await processor.process(_message(_image()))

    assert results[0].blind is False  # the degradation itself is unchanged
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "media store full" in logged
    assert "cat.png" not in logged


def test_extraction_failures_never_log_the_files_name(caplog) -> None:
    """Same rule on the extraction path: a corrupt file is said as a
    fact, with no name attached."""
    with caplog.at_level(logging.WARNING, logger="aether.connectors.attachments"):
        text = extract_doc_text("salary-negotiation.docx", "", b"not a zip file")

    assert text == ""
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "text extraction" in logged
    assert "salary-negotiation" not in logged
