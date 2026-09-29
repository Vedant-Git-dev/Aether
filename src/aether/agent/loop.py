"""The continuous loop: watch feeds, remember, reason, act — with a human
in the path for anything risky.

One background task. Each tick it (a) fires due MCP source polls (the
user's config.yaml poll_tools — a standing instruction, so these run
directly and are audited), (b) turns inbound chat into memory, (c) scores
everything new for salience, (d) checks the user's standing routines
against the new events — deterministic matching, with the taught action
passing the gate at fire time — and (e) if anything actually happened,
runs one authz-gated LLM turn and fans the reply out to every enabled
surface.

Between ticks it sleeps `agent.tick_seconds`, waking early the moment work
arrives (a message, a screen capture, a wake() from the API).

The executor is the single choke point: every ToolCall the model proposes,
every scheduled action when it fires, and every routine when it triggers,
goes through `classify` → run / park / deny, and lands in the hash-chained
audit log either way. And every act leaves a trace — what triggered it,
what the gate ruled, what came back — so "why did you do that?" always
has a record to answer from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..authz.approvals import APPROVED, DENIED, Approval, Approvals
from ..authz.audit import AuditLog
from ..authz.policy import Decision, Policy
from ..config import AppConfig, PollTool
from ..connectors.base import (
    ConnectorUnavailableError,
    InboundMessage,
    MessagingConnector,
    UnknownToolError,
)
from ..connectors.mcp_host import MCPHost
from ..connectors.registry import ToolRegistry
from ..llm.agent import run_tool_loop
from ..llm.registry import ProviderRegistry
from ..llm.types import Message, ToolCall, ToolResult
from ..memory.context import ContextBuilder
from ..memory.entities import Sender
from ..memory.events import Event, EventStore
from ..memory.salience import Salience
from ..routines import Routines, trigger_matches
from ..scheduler.jobs import ScheduledAction, Scheduler
from .prompts import SYSTEM_PROMPT
from .traces import CARRY_OUT, ROUTINE, SCHEDULED, TURN, Traces

log = logging.getLogger("aether.agent")

_MAX_OBSERVATIONS = 20

# consecutive failed polls before a connector is announced as down. The user
# hears about the state once, and once again when it recovers — never per
# failure. A server with no configured poll has no heartbeat to watch.
POLL_FAILURES_BEFORE_DOWN = 2


def _parse_quiet_hours(raw: str | None) -> tuple[int, int] | None:
    """Parse "HH:MM-HH:MM" (local time) into minutes since midnight. The
    window may wrap midnight ("23:00-08:00"); a malformed value is logged
    and ignored — a budget the config file can't explain is worse than none."""
    if not raw:
        return None
    start_text, sep, end_text = raw.partition("-")
    try:
        if not sep:
            raise ValueError(f"quiet_hours {raw!r} must look like 'HH:MM-HH:MM'")
        start_h, start_m = (int(part) for part in start_text.strip().split(":"))
        end_h, end_m = (int(part) for part in end_text.strip().split(":"))
        window = (start_h * 60 + start_m, end_h * 60 + end_m)
        if not all(0 <= m < 24 * 60 for m in window):
            raise ValueError(f"quiet_hours {raw!r} is outside a day")
        return window
    except ValueError as exc:
        log.warning("%s — quiet hours disabled", exc)
        return None


class CaptureRequestBox:
    """A one-slot mailbox: the agent asks for a screenshot, the companion
    (polling GET /api/screen-request) takes the request and captures."""

    def __init__(self) -> None:
        self._note: str | None = None

    def set(self, note: str = "") -> None:
        self._note = note.strip()

    def take(self) -> str | None:
        note = self._note
        self._note = None
        return note


class SurfaceFanout:
    """Everything the agent says reaches every enabled surface. One dying
    surface never takes the loop down with it."""

    def __init__(
        self,
        connectors: list[MessagingConnector] | None = None,
        hub: Any = None,
    ) -> None:
        self.connectors = list(connectors or [])
        self.hub = hub

    def add_connectors(self, connectors: list[MessagingConnector]) -> None:
        self.connectors.extend(connectors)

    async def send_to_user(self, text: str) -> None:
        if self.hub is not None:
            try:
                await self.hub.broadcast(text)
            except Exception:
                log.exception("web chat broadcast failed")
        for connector in self.connectors:
            try:
                await connector.send_to_user(text)
            except Exception:
                log.exception("%s send failed — continuing", connector.name)

    async def present_approval(self, approval_id: int, tool_name: str, summary: str) -> None:
        for connector in self.connectors:
            try:
                await connector.present_approval(approval_id, tool_name, summary)
            except Exception:
                log.exception("%s approval presentation failed", connector.name)
        if not self.connectors and self.hub is None:
            log.warning("approval #%d for %s has no surface to appear on", approval_id, tool_name)


def _summarize_call(call: ToolCall) -> str:
    blob = json.dumps(call.arguments, ensure_ascii=False, default=str)
    return f"{call.name} {blob[:180]}"


def _delivered_by_tool(calls: list[dict[str, Any]]) -> bool:
    """True when the turn already put words in front of the user through
    send_chat_message — a send that actually delivered (allowed, no error,
    non-empty text), not one that was denied, parked, or refused."""
    for call in calls:
        if call.get("name") != "send_chat_message":
            continue
        if call.get("decision") != "allow" or call.get("is_error"):
            continue
        if not str(call.get("params", {}).get("text", "")).strip():
            continue  # the handler refuses empty text; nothing went out
        return True
    return False


class AgentLoop:
    def __init__(
        self,
        *,
        providers: ProviderRegistry | None,
        tools: ToolRegistry,
        policy: Policy,
        approvals: Approvals,
        audit: AuditLog,
        events: EventStore,
        salience: Salience,
        context: ContextBuilder,
        scheduler: Scheduler,
        surfaces: SurfaceFanout,
        capture_box: CaptureRequestBox,
        config: AppConfig,
        host: MCPHost | None = None,
        routines: Routines | None = None,
        traces: Traces | None = None,
    ) -> None:
        self._providers = providers
        self._tools = tools
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._events = events
        self._salience = salience
        self._context = context
        self._scheduler = scheduler
        self._surfaces = surfaces
        self._capture_box = capture_box
        self._config = config
        self._host = host
        self._routines = routines
        self._traces = traces

        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._woken = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_event_id = 0
        self._poll_targets: dict[str, tuple[str, PollTool]] = {}
        self._next_poll: dict[str, datetime] = {}
        self._poll_failures: dict[str, int] = {}
        self._down: set[str] = set()
        self._quiet = _parse_quiet_hours(config.agent.quiet_hours)
        self._held: list[str] = []  # unprompted notes waiting out the window
        if host is not None:
            now = datetime.now(UTC)
            for conn in host.connections:
                for i, poll in enumerate(conn.poll_tools):
                    key = f"{conn.name}:{poll.tool}:{i}"
                    self._poll_targets[key] = (conn.name, poll)
                    self._next_poll[key] = now  # the first tick runs every poll once

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self.run(), name="agent-loop")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        self._woken.set()
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)

    def notify(self) -> None:
        """Wake the loop early — used after screen captures, poll results,
        anything the API layer knows the agent will want to see now."""
        self._woken.set()

    def submit_message(self, message: InboundMessage) -> None:
        """Queue an inbound chat message — sync, for the WebSocket path and
        tests. Connectors get the awaitable handle_inbound instead, because
        the InboundHandler contract they call through is async."""
        self._queue.put_nowait(message)
        self._woken.set()

    async def handle_inbound(self, message: InboundMessage) -> None:
        """Connector-facing inbound entry — satisfies the async InboundHandler
        contract (connectors await their handler; a sync return is the
        "'NoneType' object can't be awaited" crash)."""
        self.submit_message(message)

    # -- the interruption budget ------------------------------------------------

    def _quiet_now(self) -> bool:
        """True when local time is inside the quiet window — the user means
        *their* night, so this reads the clock naively."""
        if self._quiet is None:
            return False
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        start, end = self._quiet
        if start == end:
            return True  # a degenerate window is a 24h hold — config's choice
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end  # wraps midnight

    async def _notify(self, text: str, *, urgent: float = 0.0) -> None:
        """Send an unprompted note under the interruption budget: during
        quiet hours it waits (batched, shipped when the window ends) unless
        it is urgent enough on the 0-10 salience scale. Solicited words —
        replies to the user, approvals, scheduled deliveries — never come
        through here."""
        if not text.strip():
            return
        if self._quiet_now() and urgent < self._config.agent.quiet_urgent_salience:
            self._held.append(text)
            log.info("held for quiet hours: %s", text[:80])
            return
        await self._surfaces.send_to_user(text)

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> None:
        self._last_event_id = await self._events.max_id()
        log.info(
            "agent loop started (tick=%ss, last event id %d)",
            self._config.agent.tick_seconds,
            self._last_event_id,
        )
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), timeout=self._config.agent.tick_seconds)
            self._woken.clear()

    async def _tick(self) -> None:
        anchor = self._last_event_id

        # (a) due source polls first, so their results are observed this tick
        await self._run_due_polls()

        # (b) inbound chat becomes memory (allowlist applies at ingest; a
        # dropped sender is never seen by the model at all)
        drained: list[InboundMessage] = []
        while not self._queue.empty():
            drained.append(self._queue.get_nowait())
        messages: list[InboundMessage] = []
        for message in drained:
            result = await self._events.ingest(
                source=message.surface,
                kind="chat_message",
                payload={"text": message.text, "chat_ref": message.chat_ref},
                sender=Sender(platform=message.surface, handle=message.handle),
            )
            if result.stored and result.event_id is not None:
                await self._salience.score_event(result.event_id)
                self._last_event_id = max(self._last_event_id, result.event_id)
            if result.reason == "filtered:contacts":
                log.info(
                    "inbound from %s on %s filtered by the allowlist",
                    message.handle,
                    message.surface,
                )
                continue
            messages.append(message)

        # (c) everything else ingested since last tick becomes observations
        observations = await self._events.list_since(anchor, limit=_MAX_OBSERVATIONS)
        for event in observations:
            self._last_event_id = max(self._last_event_id, event.id)
            await self._salience.score_event(event.id)

        # (c2) standing triggers the user taught — deterministic matching
        # over the same observations, before any LLM is involved
        if observations and self._routines is not None:
            await self._run_routines(observations)

        # held unprompted notes from the quiet window ship as one batched
        # message the moment the window ends — the budget pays out, in one
        # interruption instead of a drip
        if self._held and not self._quiet_now():
            held, self._held = self._held, []
            await self._surfaces.send_to_user(
                "While it was quiet:\n\n" + "\n\n".join(held)
            )

        if not messages and not observations:
            return  # a quiet tick: no LLM turn, no tokens spent

        await self._turn(messages, observations)

    # -- source polling -----------------------------------------------------------

    async def _run_due_polls(self) -> None:
        """Run every configured poll that is due. The polls double as the
        connector watchdog's heartbeat: consecutive failures mark a server
        down (announced once), and the next good poll announces the
        recovery."""
        if not self._poll_targets:
            return
        now = datetime.now(UTC)
        for key, (server, poll) in list(self._poll_targets.items()):
            if now < self._next_poll.get(key, now):
                continue
            self._next_poll[key] = now + timedelta(minutes=poll.every_minutes)
            qualified = f"{server}__{poll.tool}"
            try:
                result = await self._host.call(qualified, poll.args)  # type: ignore[union-attr]
            except (UnknownToolError, ConnectorUnavailableError) as exc:
                log.warning("source poll %s failed: %s", qualified, exc)
                failures = self._poll_failures.get(server, 0) + 1
                self._poll_failures[server] = failures
                if failures >= POLL_FAILURES_BEFORE_DOWN and server not in self._down:
                    self._down.add(server)
                    await self._notify(
                        f"{server} hasn't been responding — I've paused "
                        "relying on it and will say when it's back."
                    )
                continue
            # a good poll is the heartbeat: the failure count resets, and a
            # server that was announced down gets its recovery announced
            self._poll_failures.pop(server, None)
            if server in self._down:
                self._down.discard(server)
                await self._notify(f"{server} is responding again.")
            await self._audit.append(
                actor="scheduler",
                tool_name=qualified,
                decision="allow",
                rules_matched="config:poll_tools",
                params=poll.args,
                outcome="source poll",
            )
            ingest = await self._events.ingest(
                source=server,
                kind=f"poll:{poll.tool}",
                payload={"args": poll.args, "result": result[:4000]},
            )
            if ingest.stored:
                self.notify()

    # -- routines ---------------------------------------------------------------

    async def _run_routines(self, observations: list[Event]) -> None:
        """Evaluate the user's standing triggers against this tick's new
        events. Matching is pure code — no LLM in the decision of *whether*
        to react — and each fire goes through `_execute`, so the gate
        applies at fire time, every time."""
        routines = await self._routines.list_enabled()
        if not routines:
            return
        now = datetime.now(timezone.utc)
        for routine in routines:
            if (
                routine.last_fired_at is not None
                and routine.cooldown_seconds > 0
                and now < routine.last_fired_at + timedelta(seconds=routine.cooldown_seconds)
            ):
                continue  # inside its cooldown window — a burst of similar events fires it once
            event = next(
                (e for e in observations if trigger_matches(routine.trigger, e)),
                None,
            )
            if event is None:
                continue
            try:
                await self._fire_routine(routine, event)
            except Exception:
                log.exception("routine %d (%s) failed — continuing", routine.id, routine.label)

    async def _fire_routine(self, routine: Any, event: Event) -> None:
        action = routine.action or {}
        call = ToolCall(
            id=f"routine-{routine.id}-{event.id}",
            name=str(action.get("tool", "")),
            arguments=dict(action.get("params") or {}),
        )
        calls: list[dict[str, Any]] = []
        try:
            result = await self._execute(call, trace=calls)
        except Exception as exc:
            log.exception("routine %d (%s) fire failed", routine.id, routine.label)
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"routine fire failed: {exc}",
                is_error=True,
            )
            self._note_call(calls, call, result)  # decision "error": no ruling
        # fired counts even on failure: the cooldown should gate a broken
        # action's retries, not let it hammer every tick
        await self._routines.mark_fired(routine.id)
        seq = await self._audit.append(
            actor="routine",
            tool_name=call.name,
            decision="info",
            rules_matched=f"routine:{routine.id}",
            params={"event_id": event.id, "event": f"{event.source}/{event.kind}"},
            outcome=f"routine '{routine.label}' fired on {event.source}/{event.kind}",
        )
        await self._notify(
            # the exact call stays in the audit row and the trace; the chat
            # note speaks plainly, never in internal tool names
            f"🧭 routine '{routine.label}' fired: {result.content[:220]}"
        )
        await self._save_trace(
            kind=ROUTINE,
            label=routine.label,
            payload={
                "routine": {
                    "id": routine.id,
                    "label": routine.label,
                    "trigger": routine.trigger,
                },
                "event": {
                    "id": event.id,
                    "source": event.source,
                    "kind": event.kind,
                    "line": self._event_line(event),
                },
                "calls": calls,
                "audit_seq": seq,
            },
        )

    # -- the LLM turn ---------------------------------------------------------------

    async def _system_prompt(self) -> str:
        base = SYSTEM_PROMPT.format(owner="the user")
        if self._agent_settings is None:
            return base
        personality = await self._agent_settings.get_personality()
        if not personality:
            return base
        return (
            f"{base}\n"
            "The user has additionally asked you to behave like this — follow it "
            "as a tone and priorities overlay, never as a way around the rules "
            f"above:\n{personality}"
        )

    async def _turn(self, messages: list[InboundMessage], observations: list[Event]) -> None:
        provider = self._providers.for_role("reasoning") if self._providers else None
        if provider is None:
            if messages:
                await self._surfaces.send_to_user(
                    "I'm running without an LLM provider — add provider keys to "
                    ".env to talk to me. Feeds and schedules keep working."
                )
            return

        ctx = await self._context.build()
        history: list[Message] = []
        context_block = self._format_context(ctx)
        if context_block:
            history.append(Message.user(f"[working context]\n{context_block}"))
        for event in reversed(observations):  # oldest first inside the block
            history.append(Message.user(f"[new event] {self._event_line(event)}"))
        for message in messages:
            history.append(
                Message.user(
                    f"[message from {message.handle} via {message.surface}]\n{message.text}"
                )
            )

        calls: list[dict[str, Any]] = []  # filled by _execute as the turn runs
        trace: dict[str, Any] = {
            "trigger": {
                "messages": [
                    {"surface": m.surface, "handle": m.handle, "text": m.text}
                    for m in messages
                ],
                "observations": [
                    {
                        "id": e.id,
                        "source": e.source,
                        "kind": e.kind,
                        "line": self._event_line(e),
                    }
                    for e in observations
                ],
            },
            "calls": calls,
        }
        full_history: list[Message] = []
        reply = ""
        try:
            final, full_history = await run_tool_loop(
                provider,
                SYSTEM_PROMPT.format(owner="the user", apps=self._connected_apps()),
                history,
                self._tools.specs(),
                lambda call: self._execute(call, trace=calls),
                max_iterations=self._config.agent.max_tool_iterations,
            )
            reply = final.text.strip()
        except Exception as exc:
            trace["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # persist even when the turn died mid-flight: the calls that did
            # run are already in `calls` (filled by reference), so a partial
            # record still answers "what were you doing?"
            reasoning = [
                m.text.strip()
                for m in full_history
                if m.role == "assistant" and m.text.strip()
            ]
            if reasoning and reply and reasoning[-1] == reply:
                reasoning.pop()  # the reply is stored as its own field
            if reasoning:
                trace["reasoning"] = reasoning
            if reply:
                trace["reply"] = reply
            await self._save_trace(
                kind=TURN,
                label=self._trace_label(messages, observations),
                payload=trace,
            )
        if reply and not _delivered_by_tool(calls):
            if messages:
                # the user spoke first — a reply is solicited, it never waits
                await self._surfaces.send_to_user(reply)
            else:
                # unprompted commentary: the interruption budget decides,
                # urgent = the loudest thing this turn saw (0-10 salience)
                await self._notify(
                    reply,
                    urgent=max((e.salience_score for e in observations), default=0.0),
                )
        elif reply:
            # the model already sent its words this turn via send_chat_message;
            # delivering the final reply too is what reads as a duplicate —
            # the same answer twice, differently worded. The reply stays in
            # the trace, so replay still shows how the turn ended.
            log.info("reply not sent — send_chat_message already reached the user this turn")

    def _connected_apps(self) -> str:
        """The live MCP server list for the system prompt — the model sees
        exactly what's reachable, so "can you send emails?" gets an honest
        "mail isn't connected" instead of an invented capability."""
        servers = self._tools.mcp_servers()
        return ", ".join(servers) if servers else "none"

    def _format_context(self, ctx: Any) -> str:
        lines: list[str] = []
        for event in ctx.events[:15]:
            lines.append(f"- {self._event_line(event)}")
        for note in ctx.notes:
            who = note.payload.get("handle") or "someone"
            lines.append(f"- note on {who}: {note.payload.get('note', '')}")
        if ctx.pending_approvals:
            lines.append(f"- {len(ctx.pending_approvals)} approval(s) still waiting for the user")
        if ctx.today_memorable:
            lines.append(f"- {ctx.today_memorable} memorable event(s) so far today")
        return "\n".join(lines)

    @staticmethod
    def _event_line(event: Event) -> str:
        text = json.dumps(event.payload, ensure_ascii=False, default=str)
        return (
            f"{event.occurred_at:%Y-%m-%d %H:%M} {event.source}/{event.kind}"
            f" (salience {event.salience_score:.0f}): {text[:400]}"
        )

    # -- the authorization-gated executor ---------------------------------------------

    @staticmethod
    def _note_call(
        trace: list[dict[str, Any]] | None,
        call: ToolCall,
        result: ToolResult,
        *,
        ruling: Any = None,
        audit_seq: int | None = None,
        approval_id: int | None = None,
    ) -> None:
        """Append one call's full record to the trace being built, if any —
        the gate's own view: the ruling, the audit row (or approval) it
        produced, and what came back. `trace is None` means the call isn't
        part of a traced act; tracing stays out of the decision path."""
        if trace is None:
            return
        if ruling is None:  # the call failed before the gate produced an outcome
            decision, matched_rule, reason = "error", "", ""
        else:
            decision = ruling.decision.value
            matched_rule = ruling.matched_rule
            reason = ruling.reason
        trace.append(
            {
                "id": call.id,
                "name": call.name,
                "params": dict(call.arguments),
                "decision": decision,
                "matched_rule": matched_rule,
                "reason": reason,
                "audit_seq": audit_seq,
                "approval_id": approval_id,
                "result": result.content,
                "is_error": result.is_error,
            }
        )

    def _plain_unavailable(self, name: str) -> str:
        """Plain words for a name the namespace can't run — the model relays
        these to the user verbatim, so no internal names, no error jargon.
        The trace and audit keep the exact call for replay."""
        server, _, tool_name = name.partition("__")
        if not tool_name:
            return "that action isn't available right now"
        if self._tools.has_server(server):
            return f"that action isn't available in {server} right now"
        return f"{server} isn't connected right now"

    @staticmethod
    def _plain_unresponsive(name: str) -> str:
        """Plain words for a linked app that failed to answer a call."""
        server, sep, _ = name.partition("__")
        if sep:
            return f"{server} isn't responding right now"
        return "that app isn't responding right now"

    async def _execute(
        self, call: ToolCall, trace: list[dict[str, Any]] | None = None
    ) -> ToolResult:
        """The single choke point between a proposal and the world. `trace`,
        when given, is the call-record list of the decision trace being
        built — passed explicitly because the scheduler worker and the agent
        loop run as separate tasks and must never write to a shared one.

        A call the namespace cannot run never parks: the user is not asked
        to consent to certain failure — it comes back in the same plain
        words as any other missing tool."""
        ruling = self._policy.classify(call.name, call.arguments)

        if ruling.decision is Decision.DENY:
            seq = await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="deny",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            result = ToolResult(
                tool_call_id=call.id, name=call.name,
                content=f"denied by policy: {ruling.reason}", is_error=True,
            )
            self._note_call(trace, call, result, ruling=ruling, audit_seq=seq)
            return result

        if ruling.decision is Decision.ALLOW:
            try:
                content = await self._tools.execute(call.name, call.arguments)
            except UnknownToolError:
                result = ToolResult(
                    tool_call_id=call.id, name=call.name,
                    content=self._plain_unavailable(call.name), is_error=True,
                )
                self._note_call(trace, call, result, ruling=ruling)
                return result
            except ConnectorUnavailableError:
                result = ToolResult(
                    tool_call_id=call.id, name=call.name,
                    content=self._plain_unresponsive(call.name), is_error=True,
                )
                self._note_call(trace, call, result, ruling=ruling)
                return result
            seq = await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="allow",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            result = ToolResult(tool_call_id=call.id, name=call.name, content=content)
            self._note_call(trace, call, result, ruling=ruling, audit_seq=seq)
            return result

        # a risky call the namespace can't run must never ask for consent —
        # approving certain failure is not a decision worth interrupting the
        # user for. Answer with the same plain words the ALLOW path uses;
        # the trace keeps the exact call.
        if self._tools.get(call.name) is None:
            result = ToolResult(
                tool_call_id=call.id, name=call.name,
                content=self._plain_unavailable(call.name), is_error=True,
            )
            self._note_call(trace, call, result, ruling=ruling)
            return result

        # REQUIRE_APPROVAL: park it, ask, and let the turn go on
        approval = await self._approvals.create(
            tool_name=call.name,
            params=call.arguments,
            rules_matched=ruling.matched_rule,
            note=ruling.reason,
        )
        await self._surfaces.present_approval(
            approval.id, call.name, _summarize_call(call)
        )
        result = ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=(
                f"held for approval (#{approval.id}) — the user has been asked on "
                "their chat surfaces and the web panel; it will run if they "
                "approve. Do not propose this call again."
            ),
        )
        self._note_call(trace, call, result, ruling=ruling, approval_id=approval.id)
        return result

    # -- decisions ------------------------------------------------------------------

    async def decide(self, approval_id: int, decision: str) -> Approval | None:
        """The web panel's path: decide, then carry out a fresh approval."""
        approval = await self._approvals.decide(approval_id, decision)
        if approval is not None:
            await self._carry_out(approval)
        return approval

    async def execute_decision(self, approval_id: int, decision: str) -> None:
        """The chat surfaces' path: they already persisted the decision via
        Approvals.decide; this only carries a fresh one out."""
        approval = await self._approvals.get(approval_id)
        if approval is None or approval.status not in (APPROVED, DENIED):
            return
        await self._carry_out(approval)

    async def _carry_out(self, approval: Approval) -> None:
        if approval.status == DENIED:
            await self._surfaces.send_to_user(f"Not run — {approval.tool_name} was denied.")
            return
        trace: dict[str, Any] = {
            "approval_id": approval.id,
            "tool": approval.tool_name,
            "params": dict(approval.params),
            "decided_by": approval.decided_by,
        }
        try:
            result = await self._tools.execute(approval.tool_name, approval.params)
        except UnknownToolError as exc:
            # rows parked before the up-front check — or an app unlinked
            # while one sat pending — still land here. The user hears plain
            # words; the exact name and error stay in the trace and the log.
            log.warning("approved call %s can't run: %s", approval.tool_name, exc)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            await self._surfaces.send_to_user(
                f"⚠️ the approved action couldn't run — "
                f"{self._plain_unavailable(approval.tool_name)}"
            )
        except ConnectorUnavailableError as exc:
            log.warning("approved call %s can't run: %s", approval.tool_name, exc)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            await self._surfaces.send_to_user(
                f"⚠️ the approved action couldn't run — "
                f"{self._plain_unresponsive(approval.tool_name)}"
            )
        except Exception as exc:
            log.exception("approved call %s failed to run", approval.tool_name)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            await self._surfaces.send_to_user(
                "⚠️ the approved action couldn't run — it failed unexpectedly. "
                "The decision record has exactly what happened."
            )
        else:
            await self._approvals.mark_executed(approval.id)
            trace["result"] = result
            trace["is_error"] = False
            await self._surfaces.send_to_user(
                f"✅ ran {approval.tool_name}: {result[:300]}"
            )
        await self._save_trace(
            kind=CARRY_OUT,
            label=f"approval #{approval.id} — {approval.tool_name}",
            payload=trace,
        )

    # -- scheduled actions ------------------------------------------------------------

    async def execute_scheduled(self, action: ScheduledAction) -> str:
        """What the scheduler worker runs for each due row. The payload is a
        tool call — and it passes the same gate as the model's proposals."""
        payload = action.payload or {}
        if payload.get("type") != "tool":
            raise ValueError(f"unknown scheduled payload type: {payload.get('type')!r}")
        call = ToolCall(
            id=f"sched-{action.id}",
            name=str(payload.get("tool", "")),
            arguments=dict(payload.get("params") or {}),
        )
        calls: list[dict[str, Any]] = []
        result = await self._execute(call, trace=calls)
        # recorded before any raise, so a failed fire still has its trace
        await self._save_trace(
            kind=SCHEDULED,
            label=action.label,
            payload={
                "action": {
                    "id": action.id,
                    "label": action.label,
                    "run_at": f"{action.run_at:%Y-%m-%d %H:%M:%S%z}",
                },
                "calls": calls,
                "result": result.content,
                "is_error": result.is_error,
            },
        )
        if result.is_error:
            raise RuntimeError(result.content)
        return result.content

    # -- traces -------------------------------------------------------------------

    async def _save_trace(
        self, *, kind: str, label: str, payload: dict[str, Any]
    ) -> None:
        """Persist one trace. Traces are bookkeeping, never a participant:
        if the store isn't wired or the write fails, the act the trace
        records has already happened and stays untouched."""
        if self._traces is None:
            return
        try:
            await self._traces.create(kind=kind, label=label, payload=payload)
        except Exception:
            log.exception("failed to persist %s trace — the act stands", kind)

    @staticmethod
    def _trace_label(
        messages: list[InboundMessage], observations: list[Event]
    ) -> str:
        """One human line for the trace list: the message that drove the
        turn, or what was seen if the turn was observation-only."""
        if messages:
            return messages[0].text[:48]
        return f"observed {len(observations)} new event(s)"
