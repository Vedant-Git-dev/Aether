"""The outbound guard and the inbound ledger — Phase 1 of the reply-hygiene
fixes.

The observed symptoms this file pins: an old question answered again on an
unprompted tick, a literal "[empty]" delivered and remembered as if it were
words, an empty model output leaving the message looking unanswered, and a
delivery failure fed to the model as a fresh event.

The general loop tests live in test_agent_loop.py (and stay untouched);
this file covers only the reply-hygiene contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fakes import FakeProvider, FakeTranscript
from test_agent_loop import LoopKit, _event

from aether.agent.tools import EMPTY_REPLY
from aether.connectors.base import InboundMessage
from aether.llm.types import Turn
from aether.memory.inbound import EXPIRED, FAILED, RECEIVED
from aether.outbound import is_deliverable


def _inbound(
    text: str,
    message_id: str = "",
    surface: str = "telegram",
    reply_to_id: str = "",
) -> InboundMessage:
    return InboundMessage(
        surface=surface,
        handle="@vedant",
        text=text,
        chat_ref="1",
        message_id=message_id,
        reply_to_id=reply_to_id,
    )


class _FakeDeliveries:
    """The delivery record's read side plus the two terminal marks — enough
    for the loop's code-only re-delivery path."""

    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = list(rows or [])
        self.sent: list[int] = []
        self.abandoned: list[int] = []

    async def undelivered(self, limit: int = 10) -> list[object]:
        return self.rows[:limit]

    async def mark_sent(self, delivery_id: int, message_id: str = "") -> None:
        self.sent.append(delivery_id)

    async def mark_abandoned(self, delivery_id: int, error: str = "") -> None:
        self.abandoned.append(delivery_id)


class _FailedDelivery:
    def __init__(self, delivery_id: int, body: str, status: str = "failed") -> None:
        self.id = delivery_id
        self.surface = "fake"
        self.status = status
        self.body = body
        self.created_at = datetime.now(UTC)


def _add_share_media(kit: LoopKit) -> None:
    """A delivering native tool, mirroring the real one's note."""
    from aether.llm.types import ToolSpec

    async def share(params: dict) -> str:
        await kit.loop._surfaces.send_to_user(str(params.get("text", "")).strip())
        return "Sent to the user's chat surfaces."

    kit.tools.add_native(ToolSpec(name="share_media", description="share"), share)


# ---------------------------------------------------------------------------
# the outbound guard
# ---------------------------------------------------------------------------


class TestOutboundGuard:
    def test_placeholders_are_not_deliverable(self) -> None:
        for text in ("[empty]", "<empty>", "(empty)", "None", "null", "undefined", "  None  "):
            assert not is_deliverable(text), text

    def test_blank_is_not_deliverable(self) -> None:
        assert not is_deliverable("")
        assert not is_deliverable("   \n  ")
        assert not is_deliverable(None)

    def test_real_words_and_a_sentence_that_contains_a_placeholder_are_fine(self) -> None:
        assert is_deliverable("Here is your answer.")
        assert is_deliverable("The box is [empty] because it shipped.")

    async def test_the_fanout_refuses_a_placeholder_even_from_a_direct_caller(self) -> None:
        from fakes import FakeSurfaceConnector

        from aether.agent.loop import SurfaceFanout

        connector = FakeSurfaceConnector()
        fanout = SurfaceFanout([connector])
        refs = await fanout.send_to_user("[empty]")
        assert refs == {}
        assert connector.sent == []


# ---------------------------------------------------------------------------
# empty / placeholder model output (test 13)
# ---------------------------------------------------------------------------


async def test_a_placeholder_reply_is_retried_then_a_real_answer_goes_out() -> None:
    provider = FakeProvider([Turn(text="[empty]"), Turn(text="Here is the answer")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("what's the status?"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer"]
    # the retry was a second model call, and the placeholder never entered
    # the history the model saw on either call
    assert len(provider.calls) == 2
    assert all("[empty]" not in m.text for _, messages, _ in provider.calls for m in messages)
    assert kit.traces.created[-1]["payload"]["reply"] == "Here is the answer"


async def test_a_second_placeholder_falls_back_and_records_the_failure() -> None:
    provider = FakeProvider([Turn(text="[empty]"), Turn(text="<empty>")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("what's the status?"))
    await kit.loop._tick()

    assert kit.connector.sent == [EMPTY_REPLY]
    assert kit.traces.created[-1]["payload"].get("error") == "empty model reply"
    record = (await kit.loop._inbound_states.recent())[0]
    assert record.status == FAILED
    assert record.reason == "empty model reply"


async def test_whitespace_only_output_is_treated_as_empty() -> None:
    provider = FakeProvider([Turn(text="   \n "), Turn(text="Recovered answer")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("say something"))
    await kit.loop._tick()
    assert kit.connector.sent == ["Recovered answer"]


async def test_a_placeholder_is_never_stored_in_the_outbound_transcript() -> None:
    provider = FakeProvider([Turn(text="None"), Turn(text="A real sentence")])
    transcript = FakeTranscript()
    kit = LoopKit(provider, transcript=transcript)
    kit.loop._surfaces.history = transcript
    kit.loop.submit_message(_inbound("tell me"))
    await kit.loop._tick()

    outbound = [text for _, direction, text in transcript.appended if direction == "out"]
    assert outbound == ["A real sentence"]
    assert all("None" not in text for text in outbound)


# ---------------------------------------------------------------------------
# one terminal outcome per inbound (tests 1, 2, 5)
# ---------------------------------------------------------------------------


async def test_an_answered_inbound_is_not_answered_again_by_an_unprompted_tick() -> None:
    provider = FakeProvider([Turn(text="42")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("how many commits are there on it?"))
    await kit.loop._tick()
    assert kit.connector.sent == ["42"]

    # a delivery-failure event arrives with nobody speaking: it must not
    # become a model observation or trigger a fresh answer to the old question
    kit.events.events[2] = _event(2, source="delivery", kind="delivery_failed")
    await kit.loop._tick()

    assert len(provider.calls) == 1  # no second model turn
    assert kit.connector.sent == ["42"]  # nothing about the old question


async def test_a_delivery_failure_is_retried_by_code_with_no_model_call() -> None:
    provider = FakeProvider([Turn(text="the first answer")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("say hi"))
    await kit.loop._tick()
    assert provider.calls  # one turn ran

    deliveries = _FakeDeliveries([_FailedDelivery(7, "the first answer")])
    kit.loop._deliveries = deliveries
    await kit.loop._tick()

    # the retry happened in code, and no model turn was spent on it
    assert deliveries.sent == [7]
    assert len(provider.calls) == 1


async def test_an_empty_reply_leaves_the_inbound_failed_not_received() -> None:
    provider = FakeProvider([Turn(text=""), Turn(text="")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("please answer"))
    await kit.loop._tick()

    record = (await kit.loop._inbound_states.recent())[0]
    assert record.status == FAILED
    assert kit.connector.sent == [EMPTY_REPLY]  # exactly one notice

    # a later tick does not re-answer the failed message
    await kit.loop._tick()
    assert kit.connector.sent == [EMPTY_REPLY]


async def test_the_same_platform_message_produces_one_model_turn() -> None:
    provider = FakeProvider([Turn(text="one answer")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("hello", message_id="m-1"))
    kit.loop.submit_message(_inbound("hello", message_id="m-1"))
    await kit.loop._tick()

    assert len(provider.calls) == 1
    assert kit.connector.sent == ["one answer"]
    rows = await kit.loop._inbound_states.recent()
    assert len(rows) == 1  # the duplicate never became a second row


# ---------------------------------------------------------------------------
# a stale received message is never auto-answered (test 3)
# ---------------------------------------------------------------------------


async def test_a_stale_received_message_gets_one_check_never_an_answer() -> None:
    provider = FakeProvider([])  # no turns scripted: any model call would fail
    kit = LoopKit(provider)
    states = kit.loop._inbound_states
    row = await states.record("telegram", "1", "m-old")
    assert row is not None and row.status == RECEIVED
    row.created_at = datetime.now(UTC) - timedelta(minutes=20)

    await kit.loop._tick()

    record = (await states.recent())[0]
    assert record.status == EXPIRED
    assert provider.calls == []  # never answered with a model turn
    assert len(kit.connector.sent) == 1  # one check-in

    # a second tick sends no second check
    await kit.loop._tick()
    assert len(kit.connector.sent) == 1


# ---------------------------------------------------------------------------
# delivery failures never reach the model (test 6)
# ---------------------------------------------------------------------------


async def test_a_delivery_failure_is_not_a_model_observation() -> None:
    provider = FakeProvider([Turn(text="noted")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1, source="delivery", kind="delivery_failed")
    kit.events.events[2] = _event(2)
    await kit.loop._tick()

    assert provider.calls, "the unrelated observation should still wake a turn"
    seen = " ".join(m.text for _, messages, _ in provider.calls for m in messages)
    assert "delivery_failed" not in seen


# ---------------------------------------------------------------------------
# proactive replay guards (test 4)
# ---------------------------------------------------------------------------


def _closed_rows() -> list[dict]:
    return [
        {
            "surface": "telegram",
            "direction": "in",
            "text": "how many commits are there on it?",
            "at": "",
        },
        {
            "surface": "telegram",
            "direction": "out",
            "text": "There are 42 commits on that branch.",
            "at": "",
        },
    ]


async def test_a_proactive_reply_that_echoes_a_closed_question_is_blocked() -> None:
    provider = FakeProvider([Turn(text="how many commits are there on it?")])
    kit = LoopKit(provider, transcript=FakeTranscript(_closed_rows()))
    kit.events.events[1] = _event(1)  # an unrelated feed event
    await kit.loop._tick()

    assert kit.connector.sent == []
    assert kit.traces.created[-1]["payload"].get("reply_blocked") is True


async def test_a_proactive_reply_that_duplicates_a_recent_send_is_blocked() -> None:
    provider = FakeProvider([Turn(text="There are 42 commits on that branch.")])
    kit = LoopKit(provider, transcript=FakeTranscript(_closed_rows()))
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    assert kit.connector.sent == []
    assert kit.traces.created[-1]["payload"].get("reply_blocked") is True


async def test_a_proactive_prompt_marks_the_past_closed_and_ends_on_the_instruction() -> None:
    provider = FakeProvider([Turn(text="noted")])
    kit = LoopKit(provider, transcript=FakeTranscript(_closed_rows()))
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    history = provider.calls[0][1]
    texts = [m.text for m in history]
    assert any("closed history" in text for text in texts)
    assert texts[-1].startswith("Handle only the new event")


# ---------------------------------------------------------------------------
# reply-to policy (tests 7, 8, 9, 10, 11)
# ---------------------------------------------------------------------------


async def test_a_prompt_answer_to_the_latest_message_has_no_reply_to() -> None:
    provider = FakeProvider([Turn(text="hi there")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("hello", message_id="m-1"))
    await kit.loop._tick()

    assert kit.connector.sent == ["hi there"]
    assert kit.connector.reply_tos == [None]


async def test_the_user_replied_to_a_message_so_the_answer_replies_to_it() -> None:
    provider = FakeProvider([Turn(text="the invoice is paid")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("what about this?", message_id="m-2", reply_to_id="m-1"))
    await kit.loop._tick()

    assert kit.connector.reply_tos == ["m-2"]


async def test_a_delayed_answer_quotes_the_question_it_answers() -> None:
    kit = LoopKit(FakeProvider([]))
    states = kit.loop._inbound_states
    row = await states.record("telegram", "1", "m-9")
    assert row is not None
    row.created_at = datetime.now(UTC) - timedelta(seconds=200)

    target = kit.loop._outbound_reply_target([_inbound("hello", message_id="m-9")], [row], False)
    assert target == "m-9"


async def test_two_pending_questions_are_each_answered_to_their_own_message() -> None:
    provider = FakeProvider([Turn(text="answer one"), Turn(text="answer two")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_inbound("question one", message_id="m-1"))
    kit.loop.submit_message(_inbound("question two", message_id="m-2"))
    await kit.loop._tick()

    assert kit.connector.sent == ["answer one", "answer two"]
    assert kit.connector.reply_tos == ["m-1", "m-2"]


async def test_a_proactive_alert_never_quotes_a_user_message() -> None:
    provider = FakeProvider([Turn(text="noted")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    assert kit.connector.sent == ["noted"]
    assert kit.connector.reply_tos == [None]
