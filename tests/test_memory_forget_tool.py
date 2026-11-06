"""The memory_forget tool: the owner's words, and only the owner's.

Plan.md section 12.11 — an instruction inside an email must never delete
memory, exactly as it must never arm a watch. Two deterministic gates are
pinned here: the loop reads the turn's own messages, and the tool refuses
any origin but "owner". Resolution is one explicit id, or one match from a
search; more than one asks instead of guessing.
"""

from __future__ import annotations

from types import SimpleNamespace

from aether.agent.loop import AgentLoop
from aether.agent.tools import register_native_tools
from aether.connectors.base import InboundMessage
from aether.connectors.registry import ToolRegistry
from aether.memory.control import ForgetReport


class FakeControl:
    def __init__(self) -> None:
        self.forgotten: list[tuple[str, int]] = []

    async def forget_event(self, event_id: int) -> ForgetReport:
        self.forgotten.append(("event", event_id))
        return ForgetReport(found=True, kind="event", label=f"Forgot item {event_id}.")

    async def forget_fact(self, fact_id: int) -> ForgetReport:
        self.forgotten.append(("fact", fact_id))
        return ForgetReport(found=True, kind="fact", label="Forgot the claim.")

    async def forget_commitment(self, commitment_id: int) -> ForgetReport:
        self.forgotten.append(("commitment", commitment_id))
        return ForgetReport(found=True, kind="commitment", label="Forgot the promise.")


class FakePipeline:
    def __init__(self, trace: list[dict]) -> None:
        self.trace = trace
        self.queries: list[str] = []

    async def run(self, text: str, **kwargs) -> SimpleNamespace:
        self.queries.append(text)
        return SimpleNamespace(
            packed=SimpleNamespace(trace=self.trace, gate="ok"),
        )


def _registry(control, origin, pipeline=None) -> ToolRegistry:
    registry = ToolRegistry()
    register_native_tools(
        registry=registry,
        events=None,
        entities=None,
        approvals=None,
        scheduler=None,
        surfaces=None,
        capture_box=None,
        memory_control=control,
        forget_origin=origin,
        pipeline=pipeline,
    )
    return registry


def _message(text: str) -> InboundMessage:
    return InboundMessage(surface="telegram", handle="@vedant", text=text, chat_ref="1")


async def test_an_email_driven_turn_cannot_forget_memory() -> None:
    control = FakeControl()
    registry = _registry(control, lambda: "context")

    result = await registry.execute("memory_forget", {"event_id": 9, "query": "the invoice"})

    assert "Only you can ask me to forget" in result
    assert control.forgotten == []


async def test_an_explicit_id_forgets_that_item() -> None:
    control = FakeControl()
    registry = _registry(control, lambda: "owner")

    result = await registry.execute("memory_forget", {"event_id": 9})

    assert result == "Forgot item 9."
    assert control.forgotten == [("event", 9)]


async def test_a_query_must_land_on_exactly_one_item() -> None:
    control = FakeControl()
    pipeline = FakePipeline(
        [
            {"id": 4, "kind": "mail", "source": "gmail", "line": "an email about the invoice"},
            {"id": 5, "kind": "mail", "source": "gmail", "line": "another email about the invoice"},
        ]
    )
    registry = _registry(control, lambda: "owner", pipeline)

    result = await registry.execute("memory_forget", {"query": "the invoice"})

    assert "More than one thing matches" in result
    assert "which should i forget" in result.lower()
    assert control.forgotten == []


async def test_one_match_forgets_it_and_nothing_forgets_nothing() -> None:
    control = FakeControl()
    registry = _registry(
        control,
        lambda: "owner",
        FakePipeline([{"id": 4, "kind": "mail", "source": "gmail", "line": "the invoice"}]),
    )
    assert await registry.execute("memory_forget", {"query": "invoice"}) == "Forgot item 4."
    assert control.forgotten == [("event", 4)]

    empty = _registry(control, lambda: "owner", FakePipeline([]))
    assert await empty.execute("memory_forget", {"query": "quantum"}) == (
        "I couldn't find that in memory."
    )
    assert control.forgotten == [("event", 4)]


async def test_a_summary_entry_is_never_offered_as_an_item() -> None:
    control = FakeControl()
    registry = _registry(
        control,
        lambda: "owner",
        FakePipeline(
            [{"id": 1, "kind": "summary", "source": "summary", "line": "a saved summary"}]
        ),
    )

    result = await registry.execute("memory_forget", {"query": "yesterday"})

    assert result == "I couldn't find that in memory."
    assert control.forgotten == []


def test_the_loop_reads_forget_provenance_from_its_own_messages() -> None:
    loop = SimpleNamespace(_turn_messages=[])
    # an email or a polled feed is an observation, not a message — an
    # unprompted turn carries nothing the owner said
    assert AgentLoop.forget_origin(loop) == "context"

    loop._turn_messages = [_message("what did Rahul say?")]
    assert AgentLoop.forget_origin(loop) == "context"

    loop._turn_messages = [_message("forget that email from Rahul")]
    assert AgentLoop.forget_origin(loop) == "owner"
