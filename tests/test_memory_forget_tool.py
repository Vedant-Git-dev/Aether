"""The memory_forget tool: the owner's words, and only the owner's.

An instruction inside an email must never delete memory, exactly as it must
never arm an alert. The gate is the turn's provenance, built by the harness:
only the owner's own message or an approved card may forget. Resolution is
one explicit id, or one match from a search; more than one asks instead of
guessing.
"""

from __future__ import annotations

from types import SimpleNamespace

from aether.agent.tools import register_native_tools
from aether.connectors.registry import ToolRegistry
from aether.memory.control import ForgetReport
from aether.origin import approved_call, owner_message, unprompted, use_origin


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


def _registry(control, pipeline=None) -> ToolRegistry:
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
        pipeline=pipeline,
    )
    return registry


async def test_an_email_driven_turn_cannot_forget_memory() -> None:
    control = FakeControl()
    registry = _registry(control)

    with use_origin(unprompted()):
        result = await registry.execute("memory_forget", {"event_id": 9, "query": "the invoice"})

    assert "Only you can ask me to forget" in result
    assert control.forgotten == []


async def test_an_explicit_id_forgets_that_item() -> None:
    control = FakeControl()
    registry = _registry(control)

    with use_origin(owner_message({"message_ids": ["m-1"]})):
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
    registry = _registry(control, pipeline)

    with use_origin(owner_message()):
        result = await registry.execute("memory_forget", {"query": "the invoice"})

    assert "More than one thing matches" in result
    assert "which should i forget" in result.lower()
    assert control.forgotten == []


async def test_one_match_forgets_it_and_nothing_forgets_nothing() -> None:
    control = FakeControl()
    registry = _registry(
        control,
        FakePipeline([{"id": 4, "kind": "mail", "source": "gmail", "line": "the invoice"}]),
    )
    with use_origin(approved_call(3)):
        assert await registry.execute("memory_forget", {"query": "invoice"}) == "Forgot item 4."
    assert control.forgotten == [("event", 4)]

    empty = _registry(control, FakePipeline([]))
    with use_origin(owner_message()):
        assert await empty.execute("memory_forget", {"query": "quantum"}) == (
            "I couldn't find that in memory."
        )
    assert control.forgotten == [("event", 4)]


async def test_a_summary_entry_is_never_offered_as_an_item() -> None:
    control = FakeControl()
    registry = _registry(
        control,
        FakePipeline(
            [{"id": 1, "kind": "summary", "source": "summary", "line": "a saved summary"}]
        ),
    )

    with use_origin(owner_message()):
        result = await registry.execute("memory_forget", {"query": "yesterday"})

    assert result == "I couldn't find that in memory."
    assert control.forgotten == []


def test_the_origin_is_owner_only_inside_an_owner_turn() -> None:
    assert unprompted().may_mutate is False
    assert owner_message().may_mutate is True
    assert approved_call(7).may_mutate is True

    # the context manager restores the previous value, so one turn's
    # provenance can never leak into the next tick
    with use_origin(owner_message({"message_ids": ["m-1"]})):
        assert owner_message().may_mutate is True
    assert unprompted().may_mutate is False
