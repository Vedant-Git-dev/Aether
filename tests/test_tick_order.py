"""Tick-order tests — what runs in front of a reply and what runs after it.

The latency contract these pin: a user waiting on words is never queued
behind source polls or the salience judge. Both still run every tick —
just never in front of an answer. (Pattern: test_agent_loop.py; its
harness is imported, never edited.)"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from fakes import FakeProvider
from test_agent_loop import LoopKit, _event, _msg

from aether.config import MCPServerConfig, PollTool
from aether.llm.types import Turn


class SlowSalience:
    """Salience double with a real beat in it — beside the reply path the
    beat must not delay words; inline (the unprompted budget) the tick
    must wait it out."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.scored: list[int] = []

    async def score_event(self, event_id: int) -> float:
        self.order.append("score:start")
        await asyncio.sleep(0.25)
        self.scored.append(event_id)
        self.order.append("score:done")
        return 5.0


def _tick_host(kit: LoopKit, order: list[str]) -> list[tuple[str, dict]]:
    """A host-shaped double that survives a whole _tick: the reconcile
    pass reads conn.ready, so the double carries it (not ready — nothing
    syncs, nothing announces), and the one poll is due now."""
    config = MCPServerConfig(
        name="mail",
        poll_tools=[PollTool(tool="list_unread", args={"limit": 5}, every_minutes=5.0)],
    )
    calls: list[tuple[str, dict]] = []

    async def call(qualified: str, args: dict) -> str:
        calls.append((qualified, dict(args)))
        order.append("poll")
        return "3 unread: alice re: demo, bob, carol"

    connection = SimpleNamespace(name="mail", poll_tools=config.poll_tools, ready=False)
    kit.loop._poll_targets["mail:list_unread:0"] = ("mail", config.poll_tools[0])
    kit.loop._next_poll["mail:list_unread:0"] = datetime.now(UTC)
    kit.loop._host = SimpleNamespace(connections=[connection], call=call)
    return calls


def _counting_send(kit: LoopKit, order: list[str]) -> None:
    """Record every reply into the shared order list the tests read."""
    real_send = kit.connector.send_to_user

    async def counting(text: str) -> str:
        order.append("reply")
        return await real_send(text)

    kit.connector.send_to_user = counting


async def test_a_waiting_message_defers_due_polls_until_after_the_reply() -> None:
    kit = LoopKit(FakeProvider([Turn(text="your answer")]))
    order: list[str] = []
    _counting_send(kit, order)
    calls = _tick_host(kit, order)

    kit.loop.submit_message(_msg("what's new?"))
    await kit.loop._tick()

    # the reply left first and the feed ran after it — never in front
    assert order == ["reply", "poll"]
    assert calls == [("mail__list_unread", {"limit": 5})]
    # the message was ingested before the poll, so this turn answered the
    # user without first going out to their mail
    assert [row["kind"] for row in kit.events.ingested] == ["chat_message", "poll:list_unread"]
    assert kit.connector.sent == ["your answer"]


async def test_an_idle_queue_still_polls_first() -> None:
    kit = LoopKit(FakeProvider([Turn(text="saw the mail")]))
    kit.events.events[1] = _event(1)
    order: list[str] = []
    _counting_send(kit, order)
    calls = _tick_host(kit, order)

    await kit.loop._tick()  # nothing queued — the tick belongs to the feeds

    assert order == ["poll", "reply"]
    assert calls == [("mail__list_unread", {"limit": 5})]
    # and the unprompted turn's budget still read a fresh, inline score
    assert kit.salience.scored == [1]
    assert kit.connector.sent == ["saw the mail"]


async def test_solicited_scoring_runs_beside_the_reply_not_in_front_of_it() -> None:
    kit = LoopKit(FakeProvider([Turn(text="here")]))
    order: list[str] = []
    judge = SlowSalience(order)
    kit.loop._salience = judge

    kit.loop.submit_message(_msg("hello"))
    await kit.loop._tick()

    # the tick closed with the words out and the judge's verdict still
    # pending — scoring never gated the reply
    assert kit.connector.sent == ["here"]
    assert judge.scored == []

    # the spawned scorer finishes after the tick — tracked, so it is
    # never lost, and discarded once done
    stragglers = set(kit.loop._scoring_tasks)
    if stragglers:
        await asyncio.wait(stragglers, timeout=5)
    assert judge.scored == [1]
    assert kit.loop._scoring_tasks == set()


async def test_unprompted_scoring_stays_inline_for_the_interruption_budget() -> None:
    kit = LoopKit(FakeProvider([Turn(text="worth mentioning")]))
    kit.events.events[1] = _event(1)
    order: list[str] = []
    _counting_send(kit, order)
    judge = SlowSalience(order)
    kit.loop._salience = judge

    await kit.loop._tick()

    # nobody spoke, so the budget read a fresh verdict: the judge finished
    # inline, before the turn spent a token
    assert order == ["score:start", "score:done", "reply"]
    assert judge.scored == [1]
    assert kit.connector.sent == ["worth mentioning"]


async def test_stop_waits_out_a_straggling_scorer() -> None:
    kit = LoopKit(FakeProvider([Turn(text="here")]))
    order: list[str] = []
    judge = SlowSalience(order)
    kit.loop._salience = judge

    kit.loop.submit_message(_msg("hello"))
    await kit.loop._tick()
    assert judge.scored == []  # still mid-judge when the tick closed

    await kit.loop.stop()

    # the shutdown grace let the judge finish — an event this tick
    # accepted still got its verdict
    assert judge.scored == [1]
    assert "score:done" in judge.order
