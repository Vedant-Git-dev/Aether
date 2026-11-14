"""Typed tool outcomes: the status is for code, the sentence is for people."""

from __future__ import annotations

import pytest

from aether.connectors.registry import ToolRegistry
from aether.llm.types import WireSpec
from aether.tools.result import ToolOutcome


def test_a_string_or_dict_is_coerced_to_ok() -> None:
    assert ToolOutcome.coerce("done").status == "ok"
    assert ToolOutcome.coerce({"a": 1}).message == '{"a": 1}'
    outcome = ToolOutcome.refused("no")
    assert ToolOutcome.coerce(outcome) is outcome


def test_error_statuses_and_plain_statuses_are_distinct() -> None:
    assert ToolOutcome.refused("no").is_error is True
    assert ToolOutcome.unavailable("gone").is_error is True
    assert ToolOutcome.error("broken").is_error is True
    assert ToolOutcome.ok("done").is_error is False
    assert ToolOutcome.empty("nothing").is_error is False
    assert ToolOutcome.pending("waiting").is_error is False


def test_an_unknown_status_or_empty_message_is_refused() -> None:
    with pytest.raises(ValueError):
        ToolOutcome("maybe", "x")
    with pytest.raises(ValueError):
        ToolOutcome.ok("   ")


async def test_the_registry_keeps_the_sentence_and_types_the_status() -> None:
    registry = ToolRegistry()

    async def refused(params: dict) -> ToolOutcome:
        return ToolOutcome.refused("not on your own words")

    registry.add_native(WireSpec(name="memory__forget", description="forget"), refused)

    assert await registry.execute("memory__forget", {}) == "not on your own words"
    outcome = await registry.execute_outcome("memory__forget", {})
    assert outcome.status == "refused"
    assert outcome.message == "not on your own words"


async def test_a_plain_string_handler_stays_ok() -> None:
    registry = ToolRegistry()

    async def plain(params: dict) -> str:
        return "done"

    registry.add_native(WireSpec(name="note_entity", description="note"), plain)

    outcome = await registry.execute_outcome("note_entity", {})
    assert outcome.status == "ok"
    assert outcome.is_error is False
