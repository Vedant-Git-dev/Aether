"""Typed outcomes: decisions are made in code, never in delivered text.

The observed symptoms this file pins: a promo email produced "no reply
needed ... well below your bar for a ping", a digest produced "I'm not
pinging you about it", and a failed write produced "no response needed -
already reported". Those are decisions, not messages.

The contract here is structural: a proactive turn can reach the user only
through the one send or the one ask, and quiet hours hold a low-urgency note
and batch it rather than blocking the offer.
"""

from __future__ import annotations

from fakes import FakeProvider
from test_agent_loop import LoopKit, _event, _msg

from aether.config import AgentConfig, AppConfig
from aether.llm.types import ToolCall, Turn


def _notify(text: str) -> Turn:
    return Turn(tool_calls=[ToolCall(id="n1", name="notify_user", arguments={"content": text})])


def _triage(decision: str) -> Turn:
    return Turn(
        tool_calls=[
            ToolCall(
                id="t1",
                name="decide_message",
                arguments={"decision": decision, "reason": "test"},
            )
        ]
    )


def _offered(provider: FakeProvider, call_index: int = 0) -> set[str]:
    return {tool.name for tool in provider.calls[call_index][2]}


# ---------------------------------------------------------------------------
# 1. free text on a proactive turn is internal
# ---------------------------------------------------------------------------


async def test_a_proactive_turn_that_returns_free_text_delivers_nothing() -> None:
    provider = FakeProvider(
        [Turn(text="no reply needed - well below your bar for a ping"), _triage("internal")]
    )
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == []
    payload = kit.traces.created[-1]["payload"]
    assert payload["internal"] == "no reply needed - well below your bar for a ping"
    assert payload["outcome"] == "none"
    assert payload["triage"]["delivered"] is False
    assert "reply" not in payload


async def test_a_useful_free_text_message_is_delivered_after_triage() -> None:
    note = "The invoice is due tomorrow — want me to draft the reply?"
    provider = FakeProvider([Turn(text=note), _triage("send")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == [note]
    payload = kit.traces.created[-1]["payload"]
    assert payload["outcome"] == "notify"
    assert payload["triage"]["delivered"] is True
    # the trace records that the model actually had the tool this turn
    assert payload["outcome_tools"] == ["message__ask", "message__send"]
    # the delivered words are not repeated under Internal
    assert "internal" not in payload


async def test_a_terminal_notify_is_not_triaged() -> None:
    provider = FakeProvider([_notify("the note")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == ["the note"]
    assert len(provider.calls) == 1
    assert "triage" not in kit.traces.created[-1]["payload"]


async def test_a_failed_triage_keeps_the_note_internal() -> None:
    provider = FakeProvider([Turn(text="a note that must not slip out"), Turn(text="no decision")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == []
    payload = kit.traces.created[-1]["payload"]
    assert payload["internal"] == "a note that must not slip out"
    assert payload["triage"]["delivered"] is False


# ---------------------------------------------------------------------------
# 2. an answer turn delivers reply, never internal_notes
# ---------------------------------------------------------------------------


async def test_an_answer_turn_delivers_only_reply_and_keeps_notes_internal() -> None:
    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="r1",
                        name="reply_user",
                        arguments={
                            "reply": "Here is the answer.",
                            "internal_notes": "SECRET-INTERNAL-NOTE",
                        },
                    )
                ]
            ),
            Turn(text="second answer"),
        ]
    )
    kit = LoopKit(provider)

    kit.loop.submit_message(_msg("first question"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer."]
    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "Here is the answer."
    assert payload["internal_notes"] == "SECRET-INTERNAL-NOTE"

    kit.loop.submit_message(_msg("second question"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer.", "second answer"]
    later = "\n".join(message.text for message in provider.calls[1][1])
    assert "SECRET-INTERNAL-NOTE" not in later


# ---------------------------------------------------------------------------
# 3. one send tool: the turn type chooses reply or notify
# ---------------------------------------------------------------------------


async def test_one_send_is_a_reply_when_the_user_spoke_first() -> None:
    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="message__send",
                        arguments={
                            "text": "Here is the answer.",
                            "internal_notes": "SECRET-INTERNAL-NOTE",
                        },
                    )
                ]
            ),
        ]
    )
    kit = LoopKit(provider)

    kit.loop.submit_message(_msg("first question"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer."]
    payload = kit.traces.created[-1]["payload"]
    assert payload["outcome"] == "reply"
    assert payload["internal_notes"] == "SECRET-INTERNAL-NOTE"


async def test_one_send_is_a_proactive_note_when_unprompted() -> None:
    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="message__send",
                        arguments={"text": "the invoice is due tomorrow"},
                    )
                ]
            )
        ]
    )
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == ["the invoice is due tomorrow"]
    assert kit.traces.created[-1]["payload"]["outcome"] == "notify"


async def test_the_old_mid_turn_note_tool_is_never_offered() -> None:
    # the consolidation deleted message__note/send_chat_message: a send is
    # the only door, so no unguarded side channel can exist on either turn
    solicited = FakeProvider([Turn(text="ok")])
    kit = LoopKit(solicited)
    kit.loop.submit_message(_msg("hello"))
    await kit.loop._tick()
    assert "message__note" not in _offered(solicited)
    assert "send_chat_message" not in _offered(solicited)
    assert "message__send" in _offered(solicited)

    unprompted = FakeProvider([Turn(text="ok"), _triage("internal")])
    kit = LoopKit(unprompted)
    kit.events.events[1] = _event(1)
    await kit.loop._tick()
    assert "message__note" not in _offered(unprompted)
    assert "send_chat_message" not in _offered(unprompted)
    assert "message__send" in _offered(unprompted)


# ---------------------------------------------------------------------------
# 4. quiet hours hold a proactive note, never drop it
# ---------------------------------------------------------------------------


async def test_a_proactive_send_in_quiet_hours_is_held_then_batched() -> None:
    provider = FakeProvider([_notify("saw the mail")])
    kit = LoopKit(provider, config=AppConfig(agent=AgentConfig(quiet_hours="00:00-23:59")))
    kit.events.events[1] = _event(1, salience=0.0)

    await kit.loop._tick()

    # the tool was offered and the model sent; the words wait, not vanish
    assert "message__send" in _offered(provider)
    assert kit.connector.sent == []
    assert kit.loop._held == ["saw the mail"]
    assert "chat_refs" not in kit.traces.created[-1]["payload"]

    kit.loop._config.agent.quiet_hours = None
    kit.loop.reparse_quiet_hours()
    await kit.loop._tick()  # nothing new — the batch flushes on its own

    assert kit.connector.sent == ["While it was quiet:\n\nsaw the mail"]
    assert kit.loop._held == []


async def test_an_urgent_note_breaks_quiet_hours_immediately() -> None:
    provider = FakeProvider([_notify("prod is down — on it")])
    kit = LoopKit(provider, config=AppConfig(agent=AgentConfig(quiet_hours="00:00-23:59")))
    kit.events.events[1] = _event(1, salience=9.0)

    await kit.loop._tick()

    assert kit.connector.sent == ["prod is down — on it"]
    assert kit.loop._held == []
