"""The routed pipeline as the agent actually runs it.

A question arrives, routing classifies it, retrieval packs cited memory into
the working context, the model answers, and the verifier checks the claim
before the reply leaves. These tests drive `_tick` the same way a real
surface does, with fakes for the model and the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fakes import FakeEntities, FakeProvider
from test_agent_loop import LoopKit, _msg

from aether.agent.tools import register_native_tools
from aether.config import AppConfig, MemoryConfig
from aether.llm.types import ToolCall, Turn
from aether.memory.indexes import IndexedEvent, MemoryIndex
from aether.memory.lexical import LexicalIndex
from aether.memory.pipeline import QueryPipeline
from aether.memory.retrieve import RetrievalConfig
from aether.memory.views import Views

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
INVOICE = (
    "[Mail: Invoice for October | from Rahul <rahul@example.com> | 2026-11-03] "
    "The October invoice is due on Friday and needs your approval."
)


def _pipeline(*chunks: tuple[int, int, str], events: list[IndexedEvent] | None = None):
    lexical = LexicalIndex()
    for chunk_id, event_id, text in chunks:
        lexical.add_chunk(
            chunk_id=chunk_id,
            event_id=event_id,
            ordinal=0,
            source="gmail",
            kind="mail",
            filename="",
            location="",
            occurred_at=NOW,
            text=text,
        )
    index = MemoryIndex()
    for event in events or [_event(chunk[1]) for chunk in chunks]:
        index.upsert(event)
    return QueryPipeline(
        lexical=lexical,
        index=index,
        views=Views(index, timezone="Asia/Kolkata"),
        vectors=None,
        cfg=RetrievalConfig(),
        timezone="Asia/Kolkata",
    )


def _event(event_id: int, *, summary: str = "") -> IndexedEvent:
    return IndexedEvent(
        id=event_id,
        source="gmail",
        kind="mail",
        occurred_at=NOW,
        salience_score=0.0,
        memorable=False,
        payload={"sender": "Rahul <rahul@example.com>"},
        meta={},
        summary=summary,
    )


def _config(*, pipeline: bool = True) -> AppConfig:
    return AppConfig(memory=MemoryConfig(query_pipeline=pipeline))


def _working_context(provider: FakeProvider, call_index: int = 0) -> str:
    """Everything the model saw before its Nth completion."""
    return "\n".join(
        block.text
        for message in provider.calls[call_index][1]
        for block in message.blocks
        if getattr(block, "text", "")
    )


async def test_a_question_reaches_the_model_with_cited_memory() -> None:
    provider = FakeProvider([Turn(text="Rahul's invoice is due on Friday [E1].")])
    kit = LoopKit(
        provider,
        config=_config(),
        query_pipeline=_pipeline((1, 1, INVOICE)),
    )

    kit.loop.submit_message(_msg("what did Rahul say about the invoice?"))
    await kit.loop._tick()

    context = _working_context(provider)
    assert "[E1 |" in context
    assert "invoice is due on Friday" in context
    assert kit.connector.sent == ["Rahul's invoice is due on Friday [E1]."]


async def test_an_unsupported_quote_is_regenerated_once_before_sending() -> None:
    provider = FakeProvider(
        [
            Turn(text='He said "the payment cleared yesterday" [E1].'),
            Turn(text="Rahul's invoice is due on Friday [E1]."),
        ]
    )
    kit = LoopKit(
        provider,
        config=_config(),
        query_pipeline=_pipeline((1, 1, INVOICE)),
    )

    kit.loop.submit_message(_msg("what did Rahul say about the invoice?"))
    await kit.loop._tick()

    assert len(provider.calls) == 2
    assert kit.connector.sent == ["Rahul's invoice is due on Friday [E1]."]


async def test_no_relevant_memory_tells_the_model_to_abstain() -> None:
    provider = FakeProvider([Turn(text="I don't have anything about that.")])
    kit = LoopKit(
        provider,
        config=_config(),
        query_pipeline=_pipeline((1, 1, INVOICE)),
    )

    kit.loop.submit_message(_msg("what did the board decide about quantum computing?"))
    await kit.loop._tick()

    assert "no relevant memory" in _working_context(provider).lower()
    assert kit.connector.sent == ["I don't have anything about that."]


async def test_an_unprompted_tick_still_gets_retrieval() -> None:
    from aether.memory.events import Event

    provider = FakeProvider([Turn(text="")])
    event = Event(
        id=1,
        source="gmail",
        kind="mail",
        occurred_at=NOW,
        payload={"subject": "Deploy", "body": "production is down"},
        salience_score=8.0,
        memorable=True,
        meta={},
        urgency=3,
        summary="production outage needs a fix",
    )
    kit = LoopKit(
        provider,
        config=_config(),
        query_pipeline=_pipeline(
            (
                1,
                1,
                "[Mail: Deploy | from Rahul <rahul@example.com> | 2026-11-03] production is down",
            ),
            events=[_event(1, summary="production outage needs a fix")],
        ),
    )
    kit.events.events[1] = event

    await kit.loop._tick()

    context = _working_context(provider)
    assert "attention (live, as of" in context
    assert "[E1" in context


async def test_the_old_vector_recall_still_runs_when_the_flag_is_off() -> None:
    from fakes import FakeVectorMemory

    from aether.memory.vectors import MemoryHit

    provider = FakeProvider([Turn(text="answered")])
    vectors = FakeVectorMemory(
        hits=[
            MemoryHit(
                event_id=2,
                chunk_id=2,
                source="gmail",
                kind="mail",
                filename="",
                location="",
                text="old vector memory",
                score=0.9,
                occurred_at=NOW,
            )
        ]
    )
    kit = LoopKit(
        provider,
        config=_config(pipeline=False),
        query_pipeline=_pipeline((1, 1, INVOICE)),
        vectors=vectors,
    )

    kit.loop.submit_message(_msg("what did Rahul say about the invoice?"))
    await kit.loop._tick()

    assert "old vector memory" in _working_context(provider)


async def test_memory_search_answers_without_an_embedder_when_the_pipeline_is_on() -> None:
    kit = LoopKit(
        FakeProvider([]),
        config=_config(),
        query_pipeline=_pipeline((1, 1, INVOICE)),
    )
    register_native_tools(
        registry=kit.tools,
        events=kit.events,
        entities=FakeEntities(),
        approvals=kit.approvals,
        scheduler=kit.scheduler,
        surfaces=None,
        capture_box=kit.capture_box,
        pipeline=_pipeline((1, 1, INVOICE)),
        vectors=None,
    )
    trace: list[dict] = []

    result = await kit.loop._execute(
        ToolCall(id="t1", name="memory_search", arguments={"query": "invoice"}),
        trace=trace,
    )

    assert result.is_error is False
    assert "[E1 |" in result.content
    assert trace[0]["decision"] == "allow"


async def test_memory_search_is_capped_per_turn() -> None:
    kit = LoopKit(FakeProvider([]), config=_config())
    register_native_tools(
        registry=kit.tools,
        events=kit.events,
        entities=FakeEntities(),
        approvals=kit.approvals,
        scheduler=kit.scheduler,
        surfaces=None,
        capture_box=kit.capture_box,
        pipeline=_pipeline((1, 1, INVOICE)),
        vectors=None,
    )
    trace = [
        {"name": "memory_search", "params": {}, "decision": "allow", "is_error": False}
        for _ in range(3)
    ]

    result = await kit.loop._execute(
        ToolCall(id="t1", name="memory_search", arguments={"query": "invoice"}),
        trace=trace,
    )

    assert result.is_error is True
    assert "maximum number of times" in result.content
