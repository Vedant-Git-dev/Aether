"""One turn, one terminal outcome — the reply-discipline seam.

The observed symptoms this file pins: a video that arrived with its
caption AND the same words again as a separate message (share_media's
delivery wasn't counted), a bare "OK." after an approval card (the card
is the turn's outcome; nothing else should follow), and one request
answered twice (an identical allowed send executed twice in a turn).

test_agent_loop.py owns the loop's general tests (and stays untouched);
this file covers only the one-outcome contract the fixes added.
"""

from __future__ import annotations

from fakes import FakeProvider
from test_agent_loop import LoopKit, _msg

from aether.connectors.base import ConnectorUnavailableError
from aether.llm.types import ToolCall, ToolSpec, Turn


def _add_share_media(kit: LoopKit, *, refuse: bool = False) -> None:
    """The real native tool's mirror: delivers through the loop's own
    surfaces and answers with the true delivery note — or, when the blob
    is gone, the real refusal that delivered nothing."""
    surfaces = kit.loop._surfaces

    async def share(params: dict) -> str:
        text = str(params.get("text", "")).strip()
        if not text:
            return "share_media needs text — a short note to send with the file."
        if refuse:
            return (
                "That media is no longer stored — it may have been removed "
                "to make room for newer files. Say so plainly; do not claim "
                "it was sent."
            )
        await surfaces.send_to_user(text)
        return "Sent to the user's chat surfaces."

    kit.tools.add_native(ToolSpec(name="share_media", description="share"), share)


async def test_a_share_media_delivery_suppresses_the_duplicated_reply() -> None:
    """The video arrived with its caption, then the same words again as
    their own message: share_media delivering is a delivery — the final
    reply beside it is a duplicate, and must not go out."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="share_media", arguments={"media_id": 2, "text": "the video"}
                    )
                ],
            ),
            Turn(text="the video"),
        ]
    )
    kit = LoopKit(provider)
    _add_share_media(kit)
    kit.loop.submit_message(_msg("send the video back"))
    await kit.loop._tick()

    assert kit.connector.sent == ["the video"]  # once, not twice
    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "the video"  # the trace still shows the words


async def test_a_refused_share_media_does_not_swallow_the_reply() -> None:
    """A refused share (the blob was gone) delivered nothing — the final
    reply is the turn's one delivery, and it must still go out. Delivery
    is detected by the handler's own note, never by the text the model
    proposed, or this refusal's non-empty text would read as sent."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="share_media", arguments={"media_id": 9, "text": "the video"}
                    )
                ],
            ),
            Turn(text="That file is no longer stored — please send it again."),
        ]
    )
    kit = LoopKit(provider)
    _add_share_media(kit, refuse=True)
    kit.loop.submit_message(_msg("send the video back"))
    await kit.loop._tick()

    assert kit.connector.sent == ["That file is no longer stored — please send it again."]


async def test_a_park_suppresses_the_filler_after_the_card() -> None:
    """One email request produced three messages, one of them a bare
    "OK.": the approval card is the turn's one terminal outcome — a
    final reply after it reads as filler and stays in the trace only."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"}
                    )
                ],
            ),
            Turn(text="OK."),
        ]
    )
    kit = LoopKit(provider)
    kit.add_tool("mail__send_message")
    kit.loop.submit_message(_msg("email a@b.c about the plan"))
    await kit.loop._tick()

    # the card went out; nothing else did
    assert len(kit.connector.approvals_presented) == 1
    assert kit.connector.sent == []
    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "OK."  # the trace keeps what was not sent


async def test_an_identical_send_runs_once_per_turn() -> None:
    """One request, two identical replies: the second identical allowed
    call in the same turn returns the first's result without running —
    one delivery, honestly recorded twice."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(id="c1", name="send_chat_message", arguments={"text": "on it"}),
                    ToolCall(id="c2", name="send_chat_message", arguments={"text": "on it"}),
                ],
            ),
            Turn(text="done"),
        ]
    )
    kit = LoopKit(provider)
    kit.add_send_chat_message()
    kit.loop.submit_message(_msg("remind me later"))
    await kit.loop._tick()

    # the fanout is the executor here: a second execution would put the
    # same words on the surface twice — one entry is the whole proof
    assert kit.connector.sent == ["on it"]


async def test_two_different_sends_both_deliver() -> None:
    """The dedup catches identical repeats, not a second legitimate
    send — two different texts in one turn both go out."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(id="c1", name="send_chat_message", arguments={"text": "first"}),
                    ToolCall(id="c2", name="send_chat_message", arguments={"text": "second"}),
                ],
            ),
            Turn(text="done"),
        ]
    )
    kit = LoopKit(provider)
    kit.add_send_chat_message()
    kit.loop.submit_message(_msg("tell me two things"))
    await kit.loop._tick()

    assert kit.connector.sent == ["first", "second"]


async def test_a_failed_send_stays_retryable_in_the_same_turn() -> None:
    """Only a successful first call satisfies the repeat — one that
    errored must not poison a second attempt in the same turn."""
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[
                    ToolCall(id="c1", name="send_chat_message", arguments={"text": "retry me"}),
                    ToolCall(id="c2", name="send_chat_message", arguments={"text": "retry me"}),
                ],
            ),
            Turn(text="done"),
        ]
    )
    kit = LoopKit(provider)
    fails = 1

    async def flaky(params: dict) -> str:
        nonlocal fails
        if fails:
            fails -= 1
            raise ConnectorUnavailableError("surface down for a moment")
        await kit.loop._surfaces.send_to_user(str(params.get("text", "")))
        return "Sent to the user's chat surfaces."

    kit.tools.add_native(ToolSpec(name="send_chat_message", description="send"), flaky)
    kit.loop.submit_message(_msg("say it"))
    await kit.loop._tick()

    assert kit.connector.sent == ["retry me"]  # the retry delivered
