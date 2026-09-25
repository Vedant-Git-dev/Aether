"""The continuous loop: watch feeds, remember, reason, act — with a human
in the path for anything risky.

One background task. Each tick it (a) fires due MCP source polls (the
user's config.yaml poll_tools — a standing instruction, so these run
directly and are audited), (b) turns inbound chat into memory, (c) scores
everything new for salience, and (d) if anything actually happened, runs
one authz-gated LLM turn and fans the reply out to every enabled surface.

Between ticks it sleeps `agent.tick_seconds`, waking early the moment work
arrives (a message, a screen capture, a wake() from the API).

The executor is the single choke point: every ToolCall the model proposes,
and every scheduled action when it fires, goes through `classify` → run /
park / deny, and lands in the hash-chained audit log either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
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
from ..scheduler.jobs import ScheduledAction, Scheduler
from .prompts import SYSTEM_PROMPT

log = logging.getLogger("aether.agent")

_MAX_OBSERVATIONS = 20


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
            log.warning(
                "approval #%d for %s has no surface to appear on", approval_id, tool_name
            )


def _summarize_call(call: ToolCall) -> str:
    blob = json.dumps(call.arguments, ensure_ascii=False, default=str)
    return f"{call.name} {blob[:180]}"


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

        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._woken = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_event_id = 0
        self._poll_targets: dict[str, tuple[str, PollTool]] = {}
        self._next_poll: dict[str, datetime] = {}
        if host is not None:
            now = datetime.now(timezone.utc)
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
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                pass

    def notify(self) -> None:
        """Wake the loop early — used after screen captures, poll results,
        anything the API layer knows the agent will want to see now."""
        self._woken.set()

    def submit_message(self, message: InboundMessage) -> None:
        """The one entry point for inbound chat, from every surface."""
        self._queue.put_nowait(message)
        self._woken.set()

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
            try:
                await asyncio.wait_for(
                    self._woken.wait(), timeout=self._config.agent.tick_seconds
                )
            except TimeoutError:
                pass
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

        if not messages and not observations:
            return  # a quiet tick: no LLM turn, no tokens spent

        await self._turn(messages, observations)

    # -- source polling -----------------------------------------------------------

    async def _run_due_polls(self) -> None:
        if not self._poll_targets:
            return
        now = datetime.now(timezone.utc)
        for key, (server, poll) in list(self._poll_targets.items()):
            if now < self._next_poll.get(key, now):
                continue
            self._next_poll[key] = now + timedelta(minutes=poll.every_minutes)
            qualified = f"{server}__{poll.tool}"
            try:
                result = await self._host.call(qualified, poll.args)  # type: ignore[union-attr]
            except (UnknownToolError, ConnectorUnavailableError) as exc:
                log.warning("source poll %s failed: %s", qualified, exc)
                continue
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

    # -- the LLM turn ---------------------------------------------------------------

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

        final, _ = await run_tool_loop(
            provider,
            SYSTEM_PROMPT.format(owner="the user"),
            history,
            self._tools.specs(),
            self._execute,
            max_iterations=self._config.agent.max_tool_iterations,
        )
        if final.text.strip():
            await self._surfaces.send_to_user(final.text.strip())

    def _format_context(self, ctx: Any) -> str:
        lines: list[str] = []
        for event in ctx.events[:15]:
            lines.append(f"- {self._event_line(event)}")
        for note in ctx.notes:
            who = note.payload.get("handle") or "someone"
            lines.append(f"- note on {who}: {note.payload.get('note', '')}")
        if ctx.pending_approvals:
            lines.append(
                f"- {len(ctx.pending_approvals)} approval(s) still waiting for the user"
            )
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

    async def _execute(self, call: ToolCall) -> ToolResult:
        """The single choke point between a proposal and the world."""
        ruling = self._policy.classify(call.name, call.arguments)

        if ruling.decision is Decision.DENY:
            await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="deny",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            return ToolResult(
                tool_call_id=call.id, name=call.name,
                content=f"denied by policy: {ruling.reason}", is_error=True,
            )

        if ruling.decision is Decision.ALLOW:
            try:
                result = await self._tools.execute(call.name, call.arguments)
            except UnknownToolError as exc:
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    content=f"unknown tool: {exc}", is_error=True,
                )
            except ConnectorUnavailableError as exc:
                return ToolResult(
                    tool_call_id=call.id, name=call.name,
                    content=f"connector unavailable: {exc}", is_error=True,
                )
            await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="allow",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            return ToolResult(tool_call_id=call.id, name=call.name, content=result)

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
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=(
                f"held for approval (#{approval.id}) — the user has been asked on "
                "their chat surfaces and the web panel; it will run if they "
                "approve. Do not propose this call again."
            ),
        )

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
            await self._surfaces.send_to_user(
                f"Not run — {approval.tool_name} was denied."
            )
            return
        try:
            result = await self._tools.execute(approval.tool_name, approval.params)
            await self._approvals.mark_executed(approval.id)
            await self._surfaces.send_to_user(
                f"✅ ran {approval.tool_name}: {result[:300]}"
            )
        except Exception as exc:
            log.exception("approved call %s failed to run", approval.tool_name)
            await self._surfaces.send_to_user(
                f"⚠️ {approval.tool_name} failed after approval: {exc}"
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
        result = await self._execute(call)
        if result.is_error:
            raise RuntimeError(result.content)
        return result.content
