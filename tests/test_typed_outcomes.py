"""Typed outcomes: decisions are made in code, never in delivered text.

The observed symptoms this file pins: a promo email produced "no reply
needed ... well below your bar for a ping", a digest produced "I'm not
pinging you about it", and a failed write produced "no response needed -
already reported". Those are decisions, not messages.

The contract here is structural: a proactive turn can reach the user only
through notify_user or ask_user, and notify_user is not even offered when
the code-evaluated policy says no.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fakes import FakeProvider
from test_agent_loop import LoopKit, _event, _msg

from aether.agent.outcomes import notification_keys
from aether.config import AgentConfig, AppConfig, NotificationConfig
from aether.llm.types import ToolCall, Turn
from aether.memory.events import Event
from aether.memory.notifications import MemoryNotificationStates


def _notify(text: str) -> Turn:
    return Turn(tool_calls=[ToolCall(id="n1", name="notify_user", arguments={"content": text})])


def _ask(question: str, options: list[str] | None = None) -> Turn:
    return Turn(
        tool_calls=[
            ToolCall(
                id="a1",
                name="ask_user",
                arguments={"question": question, "options": options or []},
            )
        ]
    )


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


def _action_event(
    event_id: int,
    *,
    approval_id: int = 7,
    tool: str = "mail__send_message",
    params: dict | None = None,
) -> Event:
    event = _event(event_id, source="agent", kind="action_failed")
    event.payload = {
        "tool": tool,
        "params": params or {"to": "a@b.c"},
        "approval_id": approval_id,
        "plain": "Failed — sending email (to a@b.c)",
    }
    return event


def _offered(provider: FakeProvider, call_index: int = 0) -> set[str]:
    return {tool.name for tool in provider.calls[call_index][2]}


# ---------------------------------------------------------------------------
# 1. policy says no: the action is not offered, and nothing is delivered
# ---------------------------------------------------------------------------


async def test_a_disabled_preference_never_offers_notify_and_delivers_nothing() -> None:
    provider = FakeProvider([_notify("this must not go out"), Turn(text="nor this")])
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(enabled=False)),
    )
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert "message__send" not in _offered(provider)
    assert kit.connector.sent == []
    payload = kit.traces.created[-1]["payload"]
    assert payload["notification_policy"] == {
        "allowed": False,
        "reason": "notifications are off",
    }
    assert payload["outcome"] == "none"


async def test_quiet_hours_never_offer_notify_and_deliver_nothing() -> None:
    provider = FakeProvider([_notify("this must not go out"), Turn(text="nor this")])
    kit = LoopKit(provider, config=AppConfig(agent=AgentConfig(quiet_hours="00:00-23:59")))
    kit.events.events[1] = _event(1, salience=0.0)

    await kit.loop._tick()

    assert "message__send" not in _offered(provider)
    assert kit.connector.sent == []
    assert kit.traces.created[-1]["payload"]["notification_policy"]["reason"] == "quiet hours"


async def test_an_already_reported_action_never_offers_notify() -> None:
    state = MemoryNotificationStates()
    event = _action_event(1)
    await state.mark_reported(notification_keys([event]))

    provider = FakeProvider([_notify("this must not go out"), Turn(text="nor this")])
    kit = LoopKit(provider)
    kit.loop._notification_states = state
    kit.events.events[1] = event

    await kit.loop._tick()

    assert "message__send" not in _offered(provider)
    assert kit.connector.sent == []
    assert kit.traces.created[-1]["payload"]["notification_policy"]["reason"] == "already reported"


async def test_a_proactive_question_is_not_offered_when_policy_says_no() -> None:
    provider = FakeProvider([_ask("which account?")])
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(enabled=False)),
    )
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert "message__ask" not in _offered(provider)
    assert kit.connector.sent == []


# ---------------------------------------------------------------------------
# 2. free text on a proactive turn is internal
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


async def test_triage_never_runs_when_the_policy_denies_notify() -> None:
    provider = FakeProvider([Turn(text="a useful note")])
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(enabled=False)),
    )
    kit.events.events[1] = _event(1)

    await kit.loop._tick()

    assert kit.connector.sent == []
    assert len(provider.calls) == 1  # no triage call was made
    payload = kit.traces.created[-1]["payload"]
    assert payload["outcome_tools"] == []  # the model was never offered notify
    assert "triage" not in payload


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
# 3. an answer turn delivers reply, never internal_notes
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
# 4. one send tool: the turn type chooses reply or notify
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
# 5. an action already reported is not reported again inside the window
# ---------------------------------------------------------------------------


async def test_a_reported_failure_is_not_repeated_inside_the_window_and_returns_after() -> None:
    provider = FakeProvider(
        [
            _notify("the write failed"),
            _notify("the write failed again"),
            _notify("the write failed after the window"),
        ]
    )
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(repeat_window_minutes=60)),
    )
    first = _action_event(1)
    kit.events.events[1] = first

    await kit.loop._tick()
    assert kit.connector.sent == ["the write failed"]

    # the same action, reported by a retry with a fresh event row
    kit.events.events[2] = _action_event(2)
    await kit.loop._tick()

    assert kit.connector.sent == ["the write failed"]  # not repeated
    assert "message__send" not in _offered(provider, 1)

    # outside the window the same action may be reported again
    keys = notification_keys([first])
    await kit.loop._notification_states.mark_reported(
        keys, at=datetime.now(UTC) - timedelta(minutes=61)
    )
    kit.events.events[3] = _action_event(3)
    await kit.loop._tick()

    assert kit.connector.sent == ["the write failed", "the write failed after the window"]


# ---------------------------------------------------------------------------
# 5. config changes behavior, no code change
# ---------------------------------------------------------------------------


async def test_changing_the_preference_changes_the_offer() -> None:
    provider = FakeProvider([_notify("first"), _notify("second")])
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(enabled=False)),
    )
    kit.events.events[1] = _event(1)
    await kit.loop._tick()
    assert kit.connector.sent == []

    kit.loop._config.notifications.enabled = True
    kit.events.events[2] = _event(2)
    await kit.loop._tick()

    assert "message__send" in _offered(provider, 1)
    assert kit.connector.sent == ["second"]


async def test_changing_quiet_hours_changes_the_offer() -> None:
    provider = FakeProvider([_notify("first"), _notify("second")])
    kit = LoopKit(provider, config=AppConfig(agent=AgentConfig(quiet_hours="00:00-23:59")))
    kit.events.events[1] = _event(1, salience=0.0)
    await kit.loop._tick()
    assert kit.connector.sent == []

    kit.loop._config.agent.quiet_hours = None
    kit.loop.reparse_quiet_hours()
    kit.events.events[2] = _event(2, salience=0.0)
    await kit.loop._tick()

    assert "message__send" in _offered(provider, 1)
    assert kit.connector.sent == ["second"]


async def test_changing_the_repeat_window_changes_the_offer() -> None:
    state = MemoryNotificationStates()
    event = _action_event(1)
    await state.mark_reported(notification_keys([event]))

    provider = FakeProvider([_notify("reported again")])
    kit = LoopKit(
        provider,
        config=AppConfig(notifications=NotificationConfig(repeat_window_minutes=0)),
    )
    kit.loop._notification_states = state
    kit.events.events[1] = event

    await kit.loop._tick()

    assert "message__send" in _offered(provider)
    assert kit.connector.sent == ["reported again"]
