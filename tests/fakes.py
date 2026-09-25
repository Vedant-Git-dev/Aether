"""In-memory doubles for unit tests — no network, no database, no SDK calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aether.authz.approvals import PENDING, Approval
from aether.llm.types import Message, ToolCall, ToolResult, ToolSpec, Turn
from aether.memory.context import AgentContext
from aether.memory.entities import Entity, PersonRef
from aether.memory.events import Event, IngestResult
from aether.scheduler.jobs import PENDING as JOB_PENDING, ScheduledAction


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
    """Approvals double: records decide() calls, returns a canned result.

    Also implements the rest of the Approvals surface (create/get/
    mark_executed/list_pending) so agent-loop tests can run the full
    park-then-decide flow without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.created: list[Approval] = []
        self.executed: list[int] = []
        self.result: object = object()  # what decide() returns (set to None to simulate stale)
        self._now = datetime.now(timezone.utc)

    async def create(
        self,
        *,
        tool_name: str,
        params: dict,
        actor: str = "agent",
        rules_matched: str = "",
        note: str = "",
    ) -> Approval:
        approval = Approval(
            id=len(self.created) + 1,
            tool_name=tool_name,
            params=dict(params),
            status=PENDING,
            created_at=self._now,
            expires_at=self._now + timedelta(hours=24),
            decided_at=None,
            decided_by=None,
        )
        self.created.append(approval)
        return approval

    async def decide(self, approval_id: int, decision: str, decided_by: str = "user"):
        self.calls.append((approval_id, decision))
        if self.result is None:
            return None
        if isinstance(self.result, Approval):
            return self.result  # the caller scripted the decided row
        return Approval(
            id=approval_id,
            tool_name=f"tool-{approval_id}",
            params={},
            status=decision,
            created_at=self._now,
            expires_at=self._now + timedelta(hours=24),
            decided_at=self._now,
            decided_by=decided_by,
        )

    async def get(self, approval_id: int) -> Approval | None:
        for approval in self.created:
            if approval.id == approval_id:
                return approval
        return None

    async def mark_executed(self, approval_id: int) -> None:
        self.executed.append(approval_id)

    async def list_pending(self) -> list[Approval]:
        return [a for a in self.created if a.status == PENDING]


class FakeAudit:
    """AuditLog double: records appends; recent()/verify_chain() feed the panel."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def append(
        self,
        *,
        actor: str,
        tool_name: str,
        decision: str,
        rules_matched: str = "",
        params: dict | None = None,
        outcome: str = "",
    ) -> int:
        self.entries.append(
            {
                "seq": len(self.entries) + 1,
                "actor": actor,
                "tool_name": tool_name,
                "decision": decision,
                "rules_matched": rules_matched,
                "params": dict(params or {}),
                "outcome": outcome,
                "created_at": datetime.now(timezone.utc),
            }
        )
        return len(self.entries)

    async def recent(self, limit: int = 100) -> list[dict]:
        return self.entries[-limit:]

    async def verify_chain(self) -> object:
        from aether.authz.audit import ChainVerification

        return ChainVerification(ok=True, entries=len(self.entries))


class FakeSalience:
    """Salience double: records score_event calls, returns a canned score."""

    def __init__(self, score: float = 5.0) -> None:
        self.score = score
        self.scored: list[int] = []

    async def score_event(self, event_id: int) -> float:
        self.scored.append(event_id)
        return self.score


class FakeContextBuilder:
    """ContextBuilder double: hands back a canned AgentContext, counts builds."""

    def __init__(self, context: AgentContext | None = None) -> None:
        self.context = context or AgentContext()
        self.builds = 0

    async def build(self) -> AgentContext:
        self.builds += 1
        return self.context


class FakeScheduler:
    """Scheduler double: records creates, hands back ScheduledActions."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, *, label: str, run_at, payload: dict, actor: str = "agent"):
        self.created.append({"label": label, "run_at": run_at, "payload": dict(payload)})
        return ScheduledAction(
            id=len(self.created),
            label=label,
            run_at=run_at,
            status=JOB_PENDING,
            payload=dict(payload),
            created_at=datetime.now(timezone.utc),
        )


class FakeEntities:
    """Entities double: resolve mints fresh identities, note() records."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, str]] = []
        self.notes: list[tuple[int, dict]] = []

    async def resolve(self, platform: str, handle: str, display_name: str = ""):
        self.resolved.append((platform, handle))
        return Entity(
            id=len(self.resolved),
            display_name=display_name or handle,
            confidence=1.0,
            handles=[(platform, handle)],
        )

    async def note(self, identity_id: int, payload: dict) -> None:
        self.notes.append((identity_id, dict(payload)))


class FakeSurfaceConnector:
    """MessagingConnector double: records sends and approval presentations."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.sent: list[str] = []
        self.approvals_presented: list[tuple[int, str, str]] = []

    async def send_to_user(self, text: str) -> None:
        self.sent.append(text)

    async def present_approval(self, approval_id: int, tool_name: str, summary: str) -> None:
        self.approvals_presented.append((approval_id, tool_name, summary))


class FakeSchedStore:
    """Scheduler-store double for worker tests: claims due, records marks."""

    def __init__(self, actions: list[ScheduledAction]) -> None:
        self.actions = actions
        self.marks: list[tuple[int, str]] = []

    async def overdue_pending(self) -> list[ScheduledAction]:
        now = datetime.now(timezone.utc)
        return [a for a in self.actions if a.status == JOB_PENDING and a.run_at <= now]

    async def claim_due(self, limit: int = 20) -> list[ScheduledAction]:
        now = datetime.now(timezone.utc)
        claimed = [
            a for a in self.actions
            if a.status == JOB_PENDING and a.run_at <= now
        ][:limit]
        for action in claimed:
            action.status = "running"
        return claimed

    async def mark(self, action_id: int, status: str, result_digest: str = "") -> None:
        self.marks.append((action_id, status))
        for action in self.actions:
            if action.id == action_id:
                action.status = status


class FakeEventStore:
    """EventStore double for salience-pipeline tests: canned events, canned
    per-source recent counts, recorded salience updates."""

    def __init__(
        self,
        events: dict[int, Event] | None = None,
        recent_counts: dict[str, int] | None = None,
        search_results: list[Event] | None = None,
    ) -> None:
        self.events = dict(events or {})
        self.recent_counts = dict(recent_counts or {})
        self.search_results = list(search_results or [])
        self.updates: list[tuple[int, float, bool, dict]] = []
        self.ingested: list[dict] = []
        # set to override what ingest() returns (e.g. filtered:contacts)
        self.ingest_result: IngestResult | None = None

    async def ingest(self, *, source, kind, payload, sender=None, occurred_at=None, meta=None):
        self.ingested.append(
            {"source": source, "kind": kind, "payload": payload, "sender": sender}
        )
        if self.ingest_result is not None:
            return self.ingest_result
        return IngestResult(
            stored=True,
            reason="new",
            # distinct from pre-populated event ids so both can coexist
            event_id=max(self.events, default=0) + len(self.ingested),
        )

    async def get(self, event_id: int) -> Event | None:
        return self.events.get(event_id)

    async def list_since(self, after_id: int, limit: int = 50) -> list[Event]:
        return [self.events[i] for i in sorted(self.events) if i > after_id][:limit]

    async def max_id(self) -> int:
        return max(self.events, default=0)

    async def recent(self, limit: int = 50) -> list[Event]:
        return [self.events[i] for i in sorted(self.events, reverse=True)][:limit]

    async def search(self, query: str, limit: int = 10, scan_window: int = 200) -> list[Event]:
        return self.search_results[:limit]

    async def count_recent(self, source: str, hours: float = 1.0) -> int:
        return self.recent_counts.get(source, 0)

    async def update_salience(
        self, event_id: int, score: float, memorable: bool, category: str = "", actionability: str = ""
    ) -> None:
        self.updates.append((event_id, score, memorable, {"category": category, "actionability": actionability}))
