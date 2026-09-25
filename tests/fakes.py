"""In-memory doubles for unit tests — no network, no database, no SDK calls."""

from __future__ import annotations

from aether.llm.types import Message, ToolCall, ToolResult, ToolSpec, Turn
from aether.memory.entities import PersonRef
from aether.memory.events import Event, IngestResult


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


class FakeJudge:
    """Scripted salience judge: returns judgments[i] on the i-th call."""

    def __init__(self, judgments: list[object]) -> None:
        self.judgments = list(judgments)
        self.calls: list[str] = []

    async def judge(self, text: str):
        self.calls.append(text)
        if not self.judgments:
            raise AssertionError("FakeJudge ran out of scripted judgments")
        item = self.judgments.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePersonJudge:
    """Scripted same-person judge: returns confidences[i] on the i-th call."""

    def __init__(self, confidences: list[float]) -> None:
        self.confidences = list(confidences)
        self.calls: list[tuple[PersonRef, PersonRef]] = []

    async def confirm(self, a: PersonRef, b: PersonRef) -> float:
        self.calls.append((a, b))
        if not self.confidences:
            raise AssertionError("FakePersonJudge ran out of scripted confidences")
        return self.confidences.pop(0)


class FakeRegistry:
    """ProviderRegistry double: always answers for_role() with one provider."""

    def __init__(self, provider: object | None) -> None:
        self._provider = provider

    def for_role(self, role: str):
        return self._provider


class FakeApprovals:
    """Approvals double: records decide() calls, returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.result: object = object()  # what decide() returns (set to None to simulate stale)

    async def decide(self, approval_id: int, decision: str):
        self.calls.append((approval_id, decision))
        return self.result


class FakeEventStore:
    """EventStore double for salience-pipeline tests: canned events, canned
    per-source recent counts, recorded salience updates."""

    def __init__(
        self,
        events: dict[int, Event] | None = None,
        recent_counts: dict[str, int] | None = None,
    ) -> None:
        self.events = dict(events or {})
        self.recent_counts = dict(recent_counts or {})
        self.updates: list[tuple[int, float, bool, dict]] = []
        self.ingested: list[dict] = []

    async def ingest(self, *, source, kind, payload, sender=None, occurred_at=None, meta=None):
        self.ingested.append(
            {"source": source, "kind": kind, "payload": payload, "sender": sender}
        )
        return IngestResult(stored=True, reason="new", event_id=len(self.ingested))

    async def get(self, event_id: int) -> Event | None:
        return self.events.get(event_id)

    async def count_recent(self, source: str, hours: float = 1.0) -> int:
        return self.recent_counts.get(source, 0)

    async def update_salience(
        self, event_id: int, score: float, memorable: bool, category: str = "", actionability: str = ""
    ) -> None:
        self.updates.append((event_id, score, memorable, {"category": category, "actionability": actionability}))
