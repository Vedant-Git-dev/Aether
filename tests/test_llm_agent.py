"""Tool-use loop tests with scripted providers — no network, no SDK."""

import asyncio

from fakes import CrashingExecutor, FakeExecutor, FakeProvider

from aether.llm.agent import DEFAULT_MAX_ITERATIONS, run_tool_loop
from aether.llm.types import Message, ToolCall, ToolResult, ToolSpec, Turn


def _spec(name: str = "search") -> ToolSpec:
    return ToolSpec(name=name, description="test tool", input_schema={"type": "object"})


async def test_multi_tool_call_conversation() -> None:
    provider = FakeProvider(
        [
            Turn(
                text="checking",
                tool_calls=[
                    ToolCall(id="c1", name="memory_search", arguments={"q": "x"}),
                    ToolCall(id="c2", name="note_entity", arguments={"name": "Y"}),
                ],
            ),
            Turn(text="all done"),
        ]
    )
    executor = FakeExecutor(results={"memory_search": "found 2", "note_entity": "noted"})

    final, history = await run_tool_loop(
        provider,
        "sys",
        [Message.user("go")],
        [_spec("memory_search"), _spec("note_entity")],
        executor,
    )

    assert final.text == "all done"
    assert final.tool_calls == []
    # user + assistant proposal + tool results + assistant final
    assert len(history) == 4
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]

    # both calls executed, in order
    assert [c.name for c in executor.calls] == ["memory_search", "note_entity"]

    # the second provider call saw the tool results paired with the calls
    second_call_messages = provider.calls[1][1]
    tool_msg = second_call_messages[-1]
    assert tool_msg.role == "tool"
    results = [b for b in tool_msg.blocks if isinstance(b, ToolResult)]
    assert [(r.tool_call_id, r.content) for r in results] == [
        ("c1", "found 2"),
        ("c2", "noted"),
    ]
    assistant_msg = second_call_messages[-2]
    assert [c.id for c in assistant_msg.tool_calls] == ["c1", "c2"]
    assert assistant_msg.text == "checking"


async def test_loop_forces_wrap_up_turn_at_max_iterations() -> None:
    tool_turns = [
        Turn(text="thinking", tool_calls=[ToolCall(id=f"c{i}", name="search", arguments={})])
        for i in range(3)
    ]
    provider = FakeProvider(tool_turns + [Turn(text="wrapping up")])
    executor = FakeExecutor()

    final, history = await run_tool_loop(
        provider, "sys", [Message.user("go")], [_spec()], executor, max_iterations=3
    )

    assert final.text == "wrapping up"
    assert len(executor.calls) == 3
    # 3 tool turns + 1 forced text-only turn
    assert len(provider.calls) == 4
    # the forced turn was made without tools
    assert provider.calls[3][2] == []
    # history: user, 3x (assistant + tool), final assistant
    assert [m.role for m in history] == ["user"] + ["assistant", "tool"] * 3 + ["assistant"]


async def test_model_looping_forever_still_terminates() -> None:
    # Script exactly enough turns for one full budget plus wrap-up; if the
    # loop failed to stop it would exhaust the script and raise.
    provider = FakeProvider(
        [Turn(tool_calls=[ToolCall(id="c", name="search", arguments={})])] * DEFAULT_MAX_ITERATIONS
        + [Turn(text="stopped")]
    )
    executor = FakeExecutor()

    final, _ = await run_tool_loop(provider, "sys", [Message.user("go")], [_spec()], executor)
    assert final.text == "stopped"
    assert len(executor.calls) == DEFAULT_MAX_ITERATIONS


async def test_crashing_executor_becomes_error_result() -> None:
    provider = FakeProvider(
        [
            Turn(tool_calls=[ToolCall(id="c1", name="search", arguments={})]),
            Turn(text="recovered"),
        ]
    )
    executor = CrashingExecutor()

    final, _ = await run_tool_loop(provider, "sys", [Message.user("go")], [_spec()], executor)

    assert final.text == "recovered"
    assert len(executor.calls) == 1
    # the model saw an error result, not silence
    tool_msg = provider.calls[1][1][-1]
    result = tool_msg.blocks[0]
    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "internal error" in result.content
    assert result.tool_call_id == "c1"


async def test_error_results_flow_through_untouched() -> None:
    provider = FakeProvider(
        [
            Turn(tool_calls=[ToolCall(id="c1", name="search", arguments={})]),
            Turn(text="handled it"),
        ]
    )

    async def execute(call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, name=call.name, content="boom", is_error=True)

    _, _ = await run_tool_loop(provider, "sys", [Message.user("go")], [_spec()], execute)
    tool_msg = provider.calls[1][1][-1]
    result = tool_msg.blocks[0]
    assert result.is_error is True
    assert result.content == "boom"


async def test_text_only_turn_ends_immediately() -> None:
    provider = FakeProvider([Turn(text="hi")])

    final, history = await run_tool_loop(
        provider, "sys", [Message.user("hello")], [], FakeExecutor()
    )

    assert final.text == "hi"
    assert len(provider.calls) == 1
    assert len(history) == 2


async def test_provider_extra_round_trips_through_history() -> None:
    provider = FakeProvider(
        [
            Turn(
                text="",
                tool_calls=[ToolCall(id="c1", name="search", arguments={})],
                provider_extra=[{"type": "thinking", "thinking": "deep", "signature": "s"}],
            ),
            Turn(text="done"),
        ]
    )

    _, history = await run_tool_loop(
        provider, "sys", [Message.user("go")], [_spec()], FakeExecutor()
    )

    # the assistant proposal message carries the provider payload verbatim
    assert history[1].provider_extra == [{"type": "thinking", "thinking": "deep", "signature": "s"}]


def test_default_iterations_matches_agent_config() -> None:
    from aether.config import AgentConfig

    assert AgentConfig().max_tool_iterations == DEFAULT_MAX_ITERATIONS


async def test_calls_in_one_proposal_run_side_by_side() -> None:
    order: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        order.append(f"start:{call.id}")
        if call.id == "c1":
            # c1 holds out until c2 has started — only possible when the
            # two really run together (bounded, so a regression fails the
            # order assert below instead of hanging the suite)
            for _ in range(100):
                if "start:c2" in order:
                    break
                await asyncio.sleep(0.01)
        order.append(f"end:{call.id}")
        return ToolResult(tool_call_id=call.id, name=call.name, content="ok")

    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(id="c1", name="search", arguments={"q": "a"}),
                    ToolCall(id="c2", name="search", arguments={"q": "b"}),
                ]
            ),
            Turn(text="done"),
        ]
    )

    final, _ = await run_tool_loop(provider, "sys", [Message.user("go")], [_spec()], execute)

    assert final.text == "done"
    # the second call began before the first ended — side by side, not queued
    assert order.index("start:c2") < order.index("end:c1")
    # the results keep the proposal's own order
    tool_msg = provider.calls[1][1][-1]
    results = [b for b in tool_msg.blocks if isinstance(b, ToolResult)]
    assert [r.tool_call_id for r in results] == ["c1", "c2"]


async def test_an_exact_repeat_in_one_proposal_runs_after_its_first() -> None:
    answered: list[dict] = []

    async def execute(call: ToolCall) -> ToolResult:
        # the repeat guard every real executor runs: a call identical to
        # one already answered gets its own refusal, on its own record
        seen = any(a["name"] == call.name and a["args"] == call.arguments for a in answered)
        answered.append({"name": call.name, "args": dict(call.arguments)})
        if seen:
            return ToolResult(
                tool_call_id=call.id, name=call.name, content="already did that this turn"
            )
        return ToolResult(tool_call_id=call.id, name=call.name, content="did it")

    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(id="c1", name="search", arguments={"q": "x"}),
                    ToolCall(id="c2", name="search", arguments={"q": "x"}),
                    ToolCall(id="c3", name="note_entity", arguments={"name": "Y"}),
                ]
            ),
            Turn(text="done"),
        ]
    )

    final, _ = await run_tool_loop(
        provider, "sys", [Message.user("go")], [_spec("search"), _spec("note_entity")], execute
    )

    assert final.text == "done"
    # every call reached the executor exactly once — the repeat included,
    # and only after its first finished
    assert [a["name"] for a in answered] == ["search", "note_entity", "search"]
    tool_msg = provider.calls[1][1][-1]
    results = [b for b in tool_msg.blocks if isinstance(b, ToolResult)]
    assert [r.tool_call_id for r in results] == ["c1", "c2", "c3"]
    assert [r.content for r in results] == ["did it", "already did that this turn", "did it"]
