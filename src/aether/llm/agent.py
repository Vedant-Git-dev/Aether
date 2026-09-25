"""The provider-agnostic tool-use loop.

Manual — not an SDK tool runner — on purpose: the loop must interpose
between "the model proposes a tool call" and "the tool executes" so every
call passes through the deterministic authorization gate, and it must
behave identically across all four providers.

The executor is injected: the caller wraps real tool execution with the
authz check, approvals, and audit before handing it here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .base import Provider
from .types import Message, ToolCall, ToolResult, ToolSpec, Turn

log = logging.getLogger("aether.llm.agent")

ToolExecutor = Callable[[ToolCall], Awaitable[ToolResult]]

DEFAULT_MAX_ITERATIONS = 12


async def run_tool_loop(
    provider: Provider,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    execute: ToolExecutor,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> tuple[Turn, list[Message]]:
    """Run reason -> act -> observe until the model replies without tool calls.

    `execute` runs each proposed ToolCall (the caller interposes the authz
    gate there) and returns a ToolResult. Tool errors are fed back to the
    model as error results instead of crashing the loop.

    Returns (final_turn, full_history): the last completion, plus the input
    messages extended with everything the loop appended.
    """
    history = list(messages)
    turn = Turn()
    for _ in range(max_iterations):
        turn = await provider.complete(system, history, tools)
        if not turn.tool_calls:
            if turn.text:
                history.append(Message.assistant(turn.text))
            return turn, history

        # Record the proposal, then execute every call and feed results back.
        history.append(
            Message.assistant(turn.text, turn.tool_calls, turn.provider_extra)
        )
        results: list[ToolResult] = []
        for call in turn.tool_calls:
            try:
                result = await execute(call)
            except Exception:
                log.exception("tool %s raised during execution", call.name)
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content="internal error: the tool raised during execution",
                    is_error=True,
                )
            results.append(result)
        history.append(Message.tool_results(results))

    # Iteration budget exhausted with the model still proposing tools: force
    # a text-only wrap-up turn so the conversation ends in words, not an
    # open tool chain.
    log.warning(
        "tool loop reached max_iterations=%d — forcing a wrap-up turn without tools",
        max_iterations,
    )
    turn = await provider.complete(system, history, tools=[])
    if turn.text:
        history.append(Message.assistant(turn.text))
    return turn, history
