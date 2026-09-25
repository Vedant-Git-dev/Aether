"""In-memory doubles for unit tests — no network, no database, no SDK calls."""

from __future__ import annotations

from aether.llm.types import Message, ToolCall, ToolResult, ToolSpec, Turn


class FakeProvider:
    """Scripted provider: returns turns[i] on the i-th complete() call.

    Records every call for assertions. A complete() with tools=[] (the
    loop's forced wrap-up) is treated like any other call, so a script must
    account for it.
    """

    name = "fake"
    supports_tools = True
    supports_vision = True

    def __init__(self, turns: list[Turn], model: str = "fake-model") -> None:
        self.turns = list(turns)
        self.model = model
        self.calls: list[tuple[str, list[Message], list[ToolSpec]]] = []

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Turn:
        self.calls.append((system, list(messages), list(tools)))
        if not self.turns:
            raise AssertionError("FakeProvider ran out of scripted turns")
        return self.turns.pop(0)


class FakeExecutor:
    """Callable tool executor returning canned results per tool name."""

    def __init__(
        self, results: dict[str, str] | None = None, default: str = "ok"
    ) -> None:
        self.results = results or {}
        self.default = default
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=self.results.get(call.name, self.default),
        )


class CrashingExecutor:
    """Executor that always raises — the loop must convert this into an
    error ToolResult instead of crashing."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        raise RuntimeError("executor exploded")
