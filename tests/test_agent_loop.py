"""Agent-loop tests — scripted FakeProvider turns, everything else faked.

The core paths: a proposal → authz ALLOW executes; a risky proposal parks
for approval and a decision runs the held call; a denied proposal never
executes; inbound chat becomes memory and reaches the model; new events
become observations; configured source polls run on schedule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aether.agent.loop import AgentLoop, CaptureRequestBox, SurfaceFanout
from aether.agent.prompts import SYSTEM_PROMPT
from aether.authz.approvals import APPROVED, DENIED
from aether.authz.policy import Policy
from aether.config import AppConfig, AuthzRule, MCPServerConfig, PollTool
from aether.connectors.base import InboundMessage
from aether.connectors.registry import ToolRegistry
from aether.llm.types import ToolCall, ToolSpec, Turn
from aether.memory.events import Event, IngestResult
from aether.scheduler.jobs import ScheduledAction
from fakes import (
    FakeApprovals,
    FakeAudit,
    FakeContextBuilder,
    FakeEventStore,
    FakeProvider,
    FakeRegistry,
    FakeRoutines,
    FakeSalience,
    FakeScheduler,
    FakeSurfaceConnector,
)


def _msg(text: str, handle: str = "@vedant", surface: str = "telegram") -> InboundMessage:
    return InboundMessage(surface=surface, handle=handle, text=text, chat_ref="1")


def _event(event_id: int, source: str = "mail", kind: str = "poll:unread") -> Event:
    return Event(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        payload={"result": "3 unread"},
        salience_score=0.0,
        memorable=False,
        meta={},
    )


class LoopKit:
    """One agent loop wired to every fake, for one test."""

    def __init__(self, provider: FakeProvider | None, *, rules: list[AuthzRule] | None = None):
        self.tools = ToolRegistry()
        self.events = FakeEventStore()
        self.approvals = FakeApprovals()
        self.audit = FakeAudit()
        self.salience = FakeSalience()
        self.context = FakeContextBuilder()
        self.scheduler = FakeScheduler()
        self.routines = FakeRoutines()
        self.connector = FakeSurfaceConnector()
        self.capture_box = CaptureRequestBox()
        self.executed: list[tuple[str, dict]] = []
        self.loop = AgentLoop(
            providers=FakeRegistry(provider) if provider else None,
            tools=self.tools,
            policy=Policy(rules or []),
            approvals=self.approvals,
            audit=self.audit,
            events=self.events,
            salience=self.salience,
            context=self.context,
            scheduler=self.scheduler,
            surfaces=SurfaceFanout([self.connector]),
            capture_box=self.capture_box,
            config=AppConfig(),
            host=None,
            routines=self.routines,
        )

    def add_tool(self, name: str, result: str = "ok") -> None:
        executed = self.executed

        async def handler(params: dict) -> str:
            executed.append((name, dict(params)))
            return result

        self.tools.add_native(ToolSpec(name=name, description=f"test {name}"), handler)


# ---------------------------------------------------------------------------
# the authz-gated executor: allow / park / deny
# ---------------------------------------------------------------------------


async def test_allow_classified_calls_execute_and_are_audited() -> None:
    kit = LoopKit(FakeProvider([Turn(text="done")]))
    kit.add_tool("mail__list_messages", result="2 unread: alice, bob")

    result = await kit.loop._execute(
        ToolCall(id="t1", name="mail__list_messages", arguments={"limit": 2})
    )
    assert result.is_error is False
    assert result.content == "2 unread: alice, bob"
    assert kit.executed == [("mail__list_messages", {"limit": 2})]
    assert kit.audit.entries[-1]["decision"] == "allow"
    assert kit.audit.entries[-1]["rules_matched"] == "builtin:read-only"


async def test_risky_calls_are_parked_and_presented_not_executed() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message")

    result = await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    assert kit.executed == []  # nothing ran
    assert len(kit.approvals.created) == 1
    approval = kit.approvals.created[0]
    assert approval.tool_name == "mail__send_message"
    assert approval.params == {"to": "a@b.c", "body": "hi"}
    assert kit.connector.approvals_presented[0][0] == approval.id
    assert "held for approval" in result.content
    assert result.is_error is False  # parked is a valid outcome, not an error


async def test_an_approved_decision_runs_the_held_call() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", result="sent")

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    approval = kit.approvals.created[0]
    approval.status = APPROVED  # the connector's decide() already flipped it

    await kit.loop.execute_decision(approval.id, APPROVED)
    assert kit.executed == [("mail__send_message", {"to": "a@b.c", "body": "hi"})]
    assert kit.approvals.executed == [approval.id]
    assert any("ran mail__send_message" in s for s in kit.connector.sent)


async def test_a_denied_decision_never_runs() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message")

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    approval = kit.approvals.created[0]
    approval.status = DENIED

    await kit.loop.execute_decision(approval.id, DENIED)
    assert kit.executed == []
    assert any("denied" in s for s in kit.connector.sent)


async def test_the_web_panel_decide_path_carries_out_a_fresh_approval() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", result="sent")

    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    approval = kit.approvals.created[0]
    approval.status = APPROVED
    kit.approvals.result = approval  # what the store hands back on a fresh decide

    assert await kit.loop.decide(approval.id, "approve") is approval
    assert kit.executed == [("mail__send_message", {"to": "a@b.c", "body": "hi"})]
    assert kit.approvals.executed == [approval.id]
    assert any("ran mail__send_message" in s for s in kit.connector.sent)


async def test_a_stale_web_decide_touches_nothing() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message")
    kit.approvals.result = None  # already decided or expired

    assert await kit.loop.decide(1, "approve") is None
    assert kit.executed == []
    assert kit.approvals.executed == []


async def test_denied_by_policy_calls_are_refused_up_front() -> None:
    kit = LoopKit(
        None,
        rules=[AuthzRule(tool_pattern="mail__send_message", decision="deny", note="no sends")],
    )
    kit.add_tool("mail__send_message")

    result = await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={})
    )
    assert result.is_error is True
    assert "denied by policy" in result.content
    assert kit.executed == []
    assert kit.audit.entries[-1]["decision"] == "deny"


async def test_unknown_and_unavailable_tools_feed_errors_back() -> None:
    kit = LoopKit(None)

    # an allow-classified name that isn't registered comes back as an error
    # result (the fail-safe default would park a truly unknown *name*)
    result = await kit.loop._execute(ToolCall(id="t1", name="mail__list_ghost", arguments={}))
    assert result.is_error is True
    assert "unknown tool" in result.content

    # a connector that is down reports unavailability, not a crash
    from aether.connectors.base import ConnectorUnavailableError

    async def down(params: dict) -> str:
        raise ConnectorUnavailableError("mail server is not connected")

    kit.tools.add_native(ToolSpec(name="mail__list_down", description=""), down)
    result = await kit.loop._execute(ToolCall(id="t2", name="mail__list_down", arguments={}))
    assert result.is_error is True
    assert "connector unavailable" in result.content


# ---------------------------------------------------------------------------
# the tick: messages, observations, quiet
# ---------------------------------------------------------------------------


async def test_inbound_message_becomes_memory_and_reaches_the_model() -> None:
    provider = FakeProvider([Turn(text="hello back")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("what do you remember?"))
    await kit.loop._tick()

    assert len(kit.events.ingested) == 1
    ingested = kit.events.ingested[0]
    assert ingested["source"] == "telegram"
    assert ingested["kind"] == "chat_message"
    assert ingested["sender"].handle == "@vedant"
    assert kit.salience.scored  # the chat was scored for memorability

    messages = provider.calls[0][1]
    assert any("what do you remember?" in m.text for m in messages)
    assert any("[message from @vedant via telegram]" in m.text for m in messages)
    assert kit.connector.sent == ["hello back"]


async def test_filtered_senders_never_reach_the_model() -> None:
    provider = FakeProvider([])  # would AssertionError if the model were called
    kit = LoopKit(provider)
    kit.events.ingest_result = IngestResult(stored=False, reason="filtered:contacts")
    kit.loop.submit_message(_msg("you should not see this"))
    await kit.loop._tick()

    assert provider.calls == []
    assert kit.connector.sent == []


async def test_new_events_become_scored_observations() -> None:
    provider = FakeProvider([Turn(text="noted the mail")])
    kit = LoopKit(provider)
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    assert kit.salience.scored == [1]
    messages = provider.calls[0][1]
    assert any("[new event]" in m.text and "poll:unread" in m.text for m in messages)


async def test_a_quiet_tick_calls_no_llm() -> None:
    provider = FakeProvider([])  # would AssertionError if called
    kit = LoopKit(provider)
    await kit.loop._tick()
    assert provider.calls == []
    assert kit.context.builds == 0


async def test_no_provider_leaves_a_note_instead_of_silence() -> None:
    kit = LoopKit(None)
    kit.loop.submit_message(_msg("hi"))
    await kit.loop._tick()
    assert any("without an LLM provider" in s for s in kit.connector.sent)


# ---------------------------------------------------------------------------
# source polls
# ---------------------------------------------------------------------------


def _poll_host(kit: LoopKit, every_minutes: float = 5.0) -> SimpleNamespace:
    """A host-shaped double: one server with one configured poll tool."""
    config = MCPServerConfig(
        name="mail",
        poll_tools=[PollTool(tool="list_unread", args={"limit": 5}, every_minutes=every_minutes)],
    )
    connection = SimpleNamespace(name="mail", poll_tools=config.poll_tools)
    calls: list[tuple[str, dict]] = []

    async def call(qualified: str, args: dict) -> str:
        calls.append((qualified, dict(args)))
        return "3 unread: alice re: demo, bob, carol"

    kit.loop._poll_targets["mail:list_unread:0"] = ("mail", config.poll_tools[0])
    kit.loop._next_poll["mail:list_unread:0"] = datetime.now(timezone.utc)
    kit.loop._host = SimpleNamespace(connections=[connection], call=call)
    return SimpleNamespace(calls=calls)


async def test_source_polls_run_ingest_and_audit() -> None:
    kit = LoopKit(FakeProvider([Turn(text="watching")]))
    host = _poll_host(kit)

    await kit.loop._run_due_polls()
    assert host.calls == [("mail__list_unread", {"limit": 5})]
    assert kit.events.ingested[-1]["source"] == "mail"
    assert kit.events.ingested[-1]["kind"] == "poll:list_unread"
    assert "3 unread" in kit.events.ingested[-1]["payload"]["result"]
    entry = kit.audit.entries[-1]
    assert entry["actor"] == "scheduler"
    assert entry["rules_matched"] == "config:poll_tools"

    # the schedule advanced — an immediate second pass does not re-poll
    await kit.loop._run_due_polls()
    assert len(host.calls) == 1


# ---------------------------------------------------------------------------
# scheduled actions
# ---------------------------------------------------------------------------


async def test_scheduled_actions_pass_the_gate_at_fire_time() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", result="sent")
    when = datetime.now(timezone.utc) + timedelta(hours=1)

    # an allowed scheduled action runs through the executor
    action = ScheduledAction(
        id=7, label="evening summary", run_at=when, status="pending",
        payload={"type": "tool", "tool": "mail__list_messages", "params": {"limit": 1}},
        created_at=when,
    )
    kit.add_tool("mail__list_messages", result="all caught up")
    content = await kit.loop.execute_scheduled(action)
    assert content == "all caught up"
    assert kit.executed[-1] == ("mail__list_messages", {"limit": 1})

    # a risky scheduled action parks for approval instead of running
    risky = ScheduledAction(
        id=8, label="send the file", run_at=when, status="pending",
        payload={"type": "tool", "tool": "mail__send_message", "params": {"to": "a@b.c"}},
        created_at=when,
    )
    content = await kit.loop.execute_scheduled(risky)
    assert "held for approval" in content
    assert kit.approvals.created[-1].tool_name == "mail__send_message"


async def test_scheduled_denials_raise_for_the_worker_to_record() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message")
    kit.loop._policy = Policy([AuthzRule(tool_pattern="mail__send_message", decision="deny")])
    action = ScheduledAction(
        id=9, label="never", run_at=datetime.now(timezone.utc), status="pending",
        payload={"type": "tool", "tool": "mail__send_message", "params": {}},
        created_at=datetime.now(timezone.utc),
    )
    try:
        await kit.loop.execute_scheduled(action)
        raise AssertionError("a denied scheduled action must raise, not pass")
    except RuntimeError as exc:
        assert "denied by policy" in str(exc)


async def test_the_system_prompt_is_formatted_safely() -> None:
    text = SYSTEM_PROMPT.format(owner="the user")
    assert "You are Aether" in text
    assert "{" not in text.replace("{owner}", "")  # no stray braces left behind
