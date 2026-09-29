"""Agent-loop tests — scripted FakeProvider turns, everything else faked.

The core paths: a proposal → authz ALLOW executes; a risky proposal parks
for approval and a decision runs the held call; a denied proposal never
executes; inbound chat becomes memory and reaches the model; new events
become observations; configured source polls run on schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aether.agent.loop import AgentLoop, CaptureRequestBox, SurfaceFanout
from aether.agent.prompts import SYSTEM_PROMPT
from aether.authz.approvals import APPROVED, DENIED
from aether.authz.audit import ChainVerification
from aether.authz.policy import Policy
from aether.config import AgentConfig, AppConfig, AuthzRule, MCPServerConfig, PollTool
from aether.connectors.base import InboundMessage
from aether.connectors.registry import ToolRegistry
from aether.llm.types import ToolCall, ToolSpec, Turn
from aether.memory.events import Event, IngestResult
from aether.scheduler.jobs import ScheduledAction
from fakes import (
    FakeAgentSettings,
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
    FakeTraces,
)


def _msg(
    text: str,
    handle: str = "@vedant",
    surface: str = "telegram",
    reply_to_id: str = "",
    reply_to_text: str = "",
) -> InboundMessage:
    return InboundMessage(
        surface=surface, handle=handle, text=text, chat_ref="1",
        reply_to_id=reply_to_id, reply_to_text=reply_to_text,
    )


def _event(
    event_id: int, source: str = "mail", kind: str = "poll:unread", salience: float = 0.0
) -> Event:
    return Event(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=datetime.now(UTC),
        payload={"result": "3 unread"},
        salience_score=salience,
        memorable=False,
        meta={},
    )


class LoopKit:
    """One agent loop wired to every fake, for one test."""

    def __init__(
        self,
        provider: FakeProvider | None,
        *,
        rules: list[AuthzRule] | None = None,
        config: AppConfig | None = None,
        agent_settings: FakeAgentSettings | None = None,
    ):
        self.tools = ToolRegistry()
        self.events = FakeEventStore()
        self.approvals = FakeApprovals()
        self.audit = FakeAudit()
        self.salience = FakeSalience()
        self.context = FakeContextBuilder()
        self.scheduler = FakeScheduler()
        self.routines = FakeRoutines()
        self.traces = FakeTraces()
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
            config=config or AppConfig(),
            host=None,
            routines=self.routines,
            traces=self.traces,
            agent_settings=agent_settings,
        )

    def add_tool(self, name: str, result: str = "ok") -> None:
        executed = self.executed

        async def handler(params: dict) -> str:
            executed.append((name, dict(params)))
            return result

        self.tools.add_native(ToolSpec(name=name, description=f"test {name}"), handler)

    def add_send_chat_message(self) -> None:
        """The real native tool's mirror: fans out through the loop's own
        surfaces and refuses empty text — so tests exercise the one-delivery
        guard against the true delivery shape."""

        async def send(params: dict) -> str:
            text = str(params.get("text", "")).strip()
            if not text:
                return "send_chat_message needs text."
            await self.loop._surfaces.send_to_user(text)
            return "Sent to the user's chat surfaces."

        self.tools.add_native(ToolSpec(name="send_chat_message", description="send"), send)


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

    result = await kit.loop._execute(ToolCall(id="t1", name="mail__send_message", arguments={}))
    assert result.is_error is True
    assert "denied by policy" in result.content
    assert kit.executed == []
    assert kit.audit.entries[-1]["decision"] == "deny"


async def test_unavailable_tools_come_back_in_plain_words() -> None:
    # the model relays these results to the user verbatim, so they carry no
    # internal tool names and no raw error text — the trace keeps the details
    kit = LoopKit(None)

    # an app that isn't linked at all (allow-classified, so the gate lets it
    # through to the registry, which is where the miss becomes a plain word)
    result = await kit.loop._execute(
        ToolCall(id="t1", name="gmail__list_messages", arguments={})
    )
    assert result.is_error is True
    assert result.content == "gmail isn't connected right now"
    assert "gmail__list_messages" not in result.content
    assert "unknown tool" not in result.content

    # a linked app that failed to answer mid-call
    from aether.connectors.base import ConnectorUnavailableError

    async def down(params: dict) -> str:
        raise ConnectorUnavailableError("mail server is not connected")

    kit.tools.add_native(ToolSpec(name="mail__list_down", description=""), down)
    result = await kit.loop._execute(ToolCall(id="t2", name="mail__list_down", arguments={}))
    assert result.is_error is True
    assert result.content == "mail isn't responding right now"
    assert "connector unavailable" not in result.content
    assert "mcp server" not in result.content.lower()


async def test_a_wrong_name_in_a_linked_app_is_plain_about_what_is_missing() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__list_messages", result="2 unread: alice, bob")

    # the model hallucinated an action a linked app doesn't offer
    result = await kit.loop._execute(
        ToolCall(id="t1", name="mail__search_ghost", arguments={})
    )
    assert result.is_error is True
    assert result.content == "that action isn't available in mail right now"
    assert "mail__search_ghost" not in result.content

    # a made-up native name gets the same treatment, without naming anything
    result = await kit.loop._execute(
        ToolCall(id="t2", name="memory_search_fast", arguments={})
    )
    assert result.is_error is True
    assert result.content == "that action isn't available right now"
    assert "memory_search_fast" not in result.content


async def test_a_risky_call_that_cannot_run_never_parks_an_approval() -> None:
    # the regression this guards: a risky name that isn't registered parked
    # an approval anyway, the user approved certain failure, and the ⚠️
    # that followed leaked the raw tool name. A call the namespace can't
    # run answers plainly up front — consent to certain failure is not a
    # decision worth interrupting the user for.
    kit = LoopKit(None)  # no servers, no tools

    result = await kit.loop._execute(
        ToolCall(id="t1", name="gmail__send_message", arguments={"to": "a@b.c"})
    )
    assert result.is_error is True
    assert result.content == "gmail isn't connected right now"
    assert "gmail__send_message" not in result.content
    assert kit.approvals.created == []  # nothing was parked
    assert kit.connector.approvals_presented == []  # and nobody was asked


async def test_a_failed_carry_out_speaks_plainly_and_closes_the_approval() -> None:
    # rows parked before the up-front check — or an app unlinked while one
    # sat pending — can still fail at carry-out: the ⚠️ speaks plainly, the
    # approval is closed as failed, and the record keeps exactly what the
    # chat line left out
    kit = LoopKit(None)
    approval = await kit.approvals.create(
        tool_name="gmail__send_message", params={"to": "a@b.c"}
    )
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    assert kit.executed == []
    assert kit.approvals.failed == [approval.id]
    assert kit.approvals.executed == []  # it didn't run; it must not read as ran

    note = kit.connector.sent[-1]
    assert "⚠️" in note
    assert "gmail isn't connected right now" in note
    assert "gmail__send_message" not in note  # never the raw name in chat
    assert "unknown tool" not in note  # never the raw error either

    payload = kit.traces.created[-1]["payload"]
    assert payload["tool"] == "gmail__send_message"  # the record keeps it
    assert "unknown tool" in payload["result"]
    assert payload["is_error"] is True


async def test_a_linked_app_that_dies_before_carry_out_is_said_plainly() -> None:
    from aether.connectors.base import ConnectorUnavailableError

    kit = LoopKit(None)

    async def down(params: dict) -> str:
        raise ConnectorUnavailableError("mail server is not connected")

    kit.tools.add_native(ToolSpec(name="mail__send_message", description=""), down)
    # registered, so the gate parks it in good faith — the server is only
    # discovered down when the approved call runs
    result = await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c"})
    )
    assert "held for approval" in result.content
    approval = kit.approvals.created[0]
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    assert kit.approvals.failed == [approval.id]
    note = kit.connector.sent[-1]
    assert "mail isn't responding right now" in note
    assert "mail__send_message" not in note
    assert "connector unavailable" not in note


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


async def test_handle_inbound_is_awaitable_and_lands_the_message() -> None:
    # Connectors await their InboundHandler; the loop's entry for them must
    # return an awaitable or every DM crashes ("'NoneType' object can't be
    # awaited") — awaiting here is the regression.
    provider = FakeProvider([Turn(text="hello back")])
    kit = LoopKit(provider)
    await kit.loop.handle_inbound(_msg("what do you remember?"))
    await kit.loop._tick()

    assert len(kit.events.ingested) == 1
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
# one delivery per turn: send_chat_message vs the final reply
# ---------------------------------------------------------------------------


async def test_a_turn_that_sent_via_tool_does_not_also_fan_out_the_reply() -> None:
    # The model can answer through send_chat_message AND write a final
    # reply — both reach the fanout, which reads as the same thing twice
    # in different wording. One delivery per turn: the tool send replaces
    # the reply, which stays in the trace for replay.
    provider = FakeProvider([
        Turn(text="", tool_calls=[
            ToolCall(id="c1", name="send_chat_message",
                     arguments={"text": "no mail server connected"})
        ]),
        Turn(text="No mail server is connected right now."),
    ])
    kit = LoopKit(provider)
    kit.add_send_chat_message()
    kit.loop.submit_message(_msg("can you mail"))
    await kit.loop._tick()

    assert kit.connector.sent == ["no mail server connected"]
    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "No mail server is connected right now."


async def test_a_denied_send_does_not_swallow_the_reply() -> None:
    # Only a send that actually delivered replaces the reply — one denied
    # by policy sent nothing, so the model's final words must still go out.
    provider = FakeProvider([
        Turn(text="", tool_calls=[
            ToolCall(id="c1", name="send_chat_message", arguments={"text": "psst"})
        ]),
        Turn(text="I couldn't send that."),
    ])
    kit = LoopKit(
        provider,
        rules=[AuthzRule(tool_pattern="send_chat_message", decision="deny", note="quiet mode")],
    )
    kit.add_send_chat_message()
    kit.loop.submit_message(_msg("tell me something"))
    await kit.loop._tick()

    assert kit.connector.sent == ["I couldn't send that."]


async def test_an_empty_send_does_not_swallow_the_reply() -> None:
    # The handler refuses empty text — nothing goes out — so the final
    # reply is still the turn's one delivery.
    provider = FakeProvider([
        Turn(text="", tool_calls=[
            ToolCall(id="c1", name="send_chat_message", arguments={"text": "  "})
        ]),
        Turn(text="Nothing to send."),
    ])
    kit = LoopKit(provider)
    kit.add_send_chat_message()
    kit.loop.submit_message(_msg("say something"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Nothing to send."]


# ---------------------------------------------------------------------------
# the interruption budget: quiet hours
# ---------------------------------------------------------------------------


def _quiet_kit(provider: FakeProvider | None) -> LoopKit:
    """A loop inside quiet hours around the clock ("00:00-23:59" is quiet for
    all but the day's final minute) — every waiting path of the budget is on."""
    return LoopKit(
        provider,
        config=AppConfig(
            agent=AgentConfig(quiet_hours="00:00-23:59", quiet_urgent_salience=8.0)
        ),
    )


async def test_quiet_hours_hold_unprompted_commentary() -> None:
    provider = FakeProvider([Turn(text="saw the mail")])
    kit = _quiet_kit(provider)
    kit.events.events[1] = _event(1)  # salience 0 — routine feed noise
    await kit.loop._tick()

    assert kit.connector.sent == []
    assert kit.loop._held == ["saw the mail"]
    # the words exist in the record — they just haven't interrupted anyone yet
    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "saw the mail"


async def test_urgent_events_still_interrupt_quiet_hours() -> None:
    provider = FakeProvider([Turn(text="prod is down — on it")])
    kit = _quiet_kit(provider)
    kit.events.events[1] = _event(1, kind="alert:down", salience=9.0)
    await kit.loop._tick()

    assert kit.connector.sent == ["prod is down — on it"]
    assert kit.loop._held == []


async def test_replies_to_the_user_never_wait_out_quiet_hours() -> None:
    provider = FakeProvider([Turn(text="here now")])
    kit = _quiet_kit(provider)
    kit.loop.submit_message(_msg("you up?"))
    await kit.loop._tick()

    assert kit.connector.sent == ["here now"]
    assert kit.loop._held == []


async def test_held_notes_ship_as_one_message_when_the_window_ends() -> None:
    provider = FakeProvider([Turn(text="first held")])
    kit = _quiet_kit(provider)
    kit.events.events[1] = _event(1)
    await kit.loop._tick()
    provider.turns.append(Turn(text="second held"))
    kit.events.events[2] = _event(2)
    await kit.loop._tick()
    assert kit.connector.sent == []
    assert kit.loop._held == ["first held", "second held"]

    # the window ends: the backlog ships as ONE message, even on a tick with
    # nothing else to say
    kit.loop._quiet = None
    await kit.loop._tick()
    assert kit.connector.sent == ["While it was quiet:\n\nfirst held\n\nsecond held"]
    assert kit.loop._held == []


async def test_a_routine_acts_at_night_but_its_note_waits() -> None:
    # a taught routine is solicited by the teaching — its action still runs
    # during quiet hours; only the meta-commentary about the fire waits
    kit = _quiet_kit(None)
    kit.add_tool("note_entity", result="noted")
    kit.routines.add(
        label="billing",
        trigger={"source": "mail"},
        action={"type": "tool", "tool": "note_entity", "params": {"note": "invoice"}},
    )
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    assert kit.executed == [("note_entity", {"note": "invoice"})]
    assert kit.connector.sent == []
    assert any("🧭" in h and "billing" in h for h in kit.loop._held)


# ---------------------------------------------------------------------------
# source polls
# ---------------------------------------------------------------------------


def _poll_host(
    kit: LoopKit, every_minutes: float = 5.0, handler=None
) -> SimpleNamespace:
    """A host-shaped double: one server with one configured poll tool.
    `handler` overrides the poll call itself (e.g. to make it fail)."""
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
    kit.loop._next_poll["mail:list_unread:0"] = datetime.now(UTC)
    kit.loop._host = SimpleNamespace(connections=[connection], call=handler or call)
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


async def test_the_watchdog_announces_a_dead_connector_once_and_its_recovery() -> None:
    # polls are the heartbeat: the first failure is silent, the second
    # announces the server is down once, further failures don't repeat it,
    # and the next good poll announces the recovery — which re-arms the
    # cycle, so a later outage is announced again.
    from aether.connectors.base import ConnectorUnavailableError

    kit = LoopKit(FakeProvider([]))
    attempts = 0

    async def flaky(qualified: str, args: dict) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 3 or attempts >= 5:
            raise ConnectorUnavailableError("mail server is not connected")
        return "3 unread: alice re: demo, bob, carol"

    _poll_host(kit, handler=flaky)
    key = "mail:list_unread:0"

    def due_again() -> None:
        kit.loop._next_poll[key] = datetime.now(UTC)

    due_again()
    await kit.loop._run_due_polls()
    assert kit.connector.sent == []  # one failure alone says nothing

    due_again()
    await kit.loop._run_due_polls()
    assert kit.connector.sent == [
        "mail hasn't been responding — I've paused relying on it and will say when it's back."
    ]

    due_again()
    await kit.loop._run_due_polls()
    assert len(kit.connector.sent) == 1  # still down, still just the one note

    due_again()
    await kit.loop._run_due_polls()
    assert kit.connector.sent[-1] == "mail is responding again."

    due_again()
    await kit.loop._run_due_polls()
    assert len(kit.connector.sent) == 2  # fresh cycle: first failure is silent

    due_again()
    await kit.loop._run_due_polls()
    assert len(kit.connector.sent) == 3  # and the second outage announces again


# ---------------------------------------------------------------------------
# scheduled actions
# ---------------------------------------------------------------------------


async def test_scheduled_actions_pass_the_gate_at_fire_time() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", result="sent")
    when = datetime.now(UTC) + timedelta(hours=1)

    # an allowed scheduled action runs through the executor
    action = ScheduledAction(
        id=7,
        label="evening summary",
        run_at=when,
        status="pending",
        payload={"type": "tool", "tool": "mail__list_messages", "params": {"limit": 1}},
        created_at=when,
    )
    kit.add_tool("mail__list_messages", result="all caught up")
    content = await kit.loop.execute_scheduled(action)
    assert content == "all caught up"
    assert kit.executed[-1] == ("mail__list_messages", {"limit": 1})

    # a risky scheduled action parks for approval instead of running
    risky = ScheduledAction(
        id=8,
        label="send the file",
        run_at=when,
        status="pending",
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
        id=9,
        label="never",
        run_at=datetime.now(UTC),
        status="pending",
        payload={"type": "tool", "tool": "mail__send_message", "params": {}},
        created_at=datetime.now(UTC),
    )
    try:
        await kit.loop.execute_scheduled(action)
        raise AssertionError("a denied scheduled action must raise, not pass")
    except RuntimeError as exc:
        assert "denied by policy" in str(exc)


# ---------------------------------------------------------------------------
# the system prompt: the model knows what it can reach
# ---------------------------------------------------------------------------


def test_the_system_prompt_is_formatted_safely() -> None:
    text = SYSTEM_PROMPT.format(owner="the user", apps="none")
    assert "You are Aether" in text
    assert "plain words" in text  # failures are worded for the user, not the log
    assert "Never invent a tool name" in text  # no promised capabilities
    assert "{" not in text.replace("{owner}", "")  # no stray braces left behind


async def test_the_prompt_tells_the_model_which_apps_are_connected() -> None:
    # "can you send emails?" with no mail server must get an honest no —
    # the prompt states the live namespace, so the model never promises a
    # capability it doesn't have
    provider = FakeProvider([Turn(text="no mail is connected right now")])
    kit = LoopKit(provider)
    kit.tools.attach_mcp(
        SimpleNamespace(
            tool_specs=lambda: [ToolSpec(name="calendar__list_events", description="")]
        )
    )
    kit.tools.sync_mcp_tools()
    kit.loop.submit_message(_msg("can you send emails?"))
    await kit.loop._tick()

    system = provider.calls[0][0]
    assert "Apps connected right now: calendar." in system
    assert "Apps connected right now: none" not in system


async def test_with_no_apps_connected_the_prompt_says_so() -> None:
    provider = FakeProvider([Turn(text="nothing is connected")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("what can you reach?"))
    await kit.loop._tick()

    assert "Apps connected right now: none." in provider.calls[0][0]


async def test_system_prompt_appends_the_owners_personality_text() -> None:
    # the personality block from the settings store rides on top of the
    # prompt — a tone overlay the owner writes, never a way around the rules
    kit = LoopKit(None, agent_settings=FakeAgentSettings("Be extremely terse. Use no emoji."))
    prompt = await kit.loop._system_prompt()
    base = SYSTEM_PROMPT.format(owner="the user", apps=kit.loop._connected_apps())
    assert prompt.startswith(base)
    assert "Be extremely terse. Use no emoji." in prompt


async def test_system_prompt_is_unchanged_with_no_personality_configured() -> None:
    kit = LoopKit(None, agent_settings=FakeAgentSettings(""))
    assert await kit.loop._system_prompt() == SYSTEM_PROMPT.format(
        owner="the user", apps=kit.loop._connected_apps()
    )


async def test_system_prompt_is_unchanged_with_no_settings_store_wired() -> None:
    kit = LoopKit(None)  # agent_settings defaults to None
    assert await kit.loop._system_prompt() == SYSTEM_PROMPT.format(
        owner="the user", apps=kit.loop._connected_apps()
    )


# ---------------------------------------------------------------------------
# the deterministic chat vocabulary: /verify and the why? reply
# ---------------------------------------------------------------------------


async def test_verify_command_answers_from_the_record_without_waking_the_model() -> None:
    provider = FakeProvider([])  # any LLM call would fail the test
    kit = LoopKit(provider)
    await kit.audit.append(actor="agent", tool_name="mail__list_unread", decision="allow")
    kit.loop.submit_message(_msg("/verify"))
    await kit.loop._tick()

    note = kit.connector.sent[-1]
    assert "🛡️" in note and "chain intact" in note
    assert "moments ago" in note  # the row was just written
    assert provider.calls == []  # deterministic — no LLM turn
    assert kit.events.ingested == []  # and nothing became memory
    assert kit.traces.created == []  # a read leaves no record of its own


async def test_verify_command_reports_a_broken_chain_in_plain_words() -> None:
    provider = FakeProvider([])  # any LLM call would fail the test
    kit = LoopKit(provider)
    kit.audit.verification = ChainVerification(
        ok=False, entries=892, first_bad_seq=892,
        problem="seq 892: stored hash does not match the entry contents",
    )
    kit.loop.submit_message(_msg("/verify"))
    await kit.loop._tick()

    note = kit.connector.sent[-1]
    assert "⚠️" in note and "BROKEN at entry #892" in note
    assert provider.calls == []


async def test_verify_command_survives_a_dead_record() -> None:
    provider = FakeProvider([])
    kit = LoopKit(provider)

    async def dead() -> object:
        raise RuntimeError("the database is down")

    kit.audit.verify_chain = dead  # type: ignore[method-assign]
    kit.loop.submit_message(_msg("/verify"))
    await kit.loop._tick()

    assert "couldn't verify" in kit.connector.sent[-1]
    assert provider.calls == []


async def test_verify_answers_even_during_quiet_hours() -> None:
    # the user asked for the check — solicited words never wait out the window
    kit = _quiet_kit(FakeProvider([]))
    await kit.audit.append(actor="agent", tool_name="t", decision="allow")
    kit.loop.submit_message(_msg("/verify"))
    await kit.loop._tick()

    assert "🛡️" in kit.connector.sent[-1]
    assert kit.loop._held == []


async def test_an_unknown_command_flows_to_the_model() -> None:
    provider = FakeProvider([Turn(text="there's no /help — just ask me")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("/help"))
    await kit.loop._tick()

    assert len(kit.events.ingested) == 1  # an unhandled command is just words
    assert any("/help" in m.text for m in provider.calls[0][1])
    assert kit.connector.sent == ["there's no /help — just ask me"]


async def test_a_replied_turn_records_where_the_words_landed() -> None:
    provider = FakeProvider([Turn(text="hello back")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("you there?"))
    await kit.loop._tick()

    payload = kit.traces.created[-1]["payload"]
    assert payload["chat_refs"] == {"fake": "m1"}  # what a later why? finds


async def test_a_held_note_records_no_chat_refs() -> None:
    provider = FakeProvider([Turn(text="saw the mail")])
    kit = _quiet_kit(provider)
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    payload = kit.traces.created[-1]["payload"]
    assert payload["reply"] == "saw the mail"
    assert "chat_refs" not in payload  # nothing landed anywhere yet


async def test_a_why_reply_is_answered_from_the_record() -> None:
    provider = FakeProvider([])  # any LLM call would fail the test
    kit = LoopKit(provider)
    kit.traces.add(
        kind="turn",
        label="email alice",
        payload={
            "trigger": {"messages": [], "observations": []},
            "calls": [],
            "reply": "on it — sent once you approve",
            "chat_refs": {"telegram": "101"},
        },
    )
    kit.loop.submit_message(_msg("why?", reply_to_id="101"))
    await kit.loop._tick()

    replay = kit.connector.sent[-1]
    assert replay.startswith("🧵 that message, from the record")
    assert "on it — sent once you approve" in replay
    assert provider.calls == []  # the record answered, not the model
    assert kit.traces.created == []  # a replay is a read — no new trace
    assert kit.events.ingested == []  # and not memory either


async def test_a_why_reply_to_an_unrecorded_message_reaches_the_model() -> None:
    provider = FakeProvider([Turn(text="I'm not sure what you mean")])
    kit = LoopKit(provider)
    kit.loop.submit_message(
        _msg("why?", reply_to_id="999", reply_to_text="the earlier words")
    )
    await kit.loop._tick()

    # nothing linked the reply, so the model answers — and it can see which
    # of its own messages the question was about
    history = provider.calls[0][1]
    assert any("(replying to my earlier message:" in m.text for m in history)
    assert kit.connector.sent == ["I'm not sure what you mean"]


async def test_a_carried_out_approval_records_where_the_news_landed() -> None:
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", result="sent")
    await kit.loop._execute(
        ToolCall(id="t1", name="mail__send_message", arguments={"to": "a@b.c", "body": "hi"})
    )
    approval = kit.approvals.created[0]
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)

    payload = kit.traces.created[-1]["payload"]
    assert payload["chat_refs"] == {"fake": "m1"}


async def test_a_routine_fire_records_where_its_ping_landed() -> None:
    kit = LoopKit(None)  # routines fire before any LLM turn
    kit.add_tool("note_entity", result="noted")
    kit.routines.add(
        label="billing",
        trigger={"source": "mail"},
        action={"type": "tool", "tool": "note_entity", "params": {"note": "invoice"}},
    )
    kit.events.events[1] = _event(1)
    await kit.loop._tick()

    payload = kit.traces.created[-1]["payload"]
    assert payload["chat_refs"] == {"fake": "m1"}
