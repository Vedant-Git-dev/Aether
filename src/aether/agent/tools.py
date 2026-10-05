"""Aether's native tools — reading its own memory, keeping entity notes,
scheduling, requesting screen captures, talking to the user, managing
the user's routines, replaying why it acted, and proving the record
intact.

These are internal by construction: they touch only Aether's own state
(or the owner's own chat surfaces), which is why the authz classifier's
`builtin:internal` rule lets them run without approval. Everything that
leaves Aether still goes through the gate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..authz.audit import verification_text
from ..connectors.registry import ToolRegistry
from ..llm.types import ToolSpec
from ..workspace import SecretRejected, WorkspaceWriteError

log = logging.getLogger("aether.agent.tools")

# what the tools need, expressed as protocols the loop actually owns
EventSearch = Callable[..., Awaitable[Any]]
NativeHandler = Callable[[dict[str, Any]], Awaitable[str]]


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# how a tool call is said in plain words — the record keeps the exact
# name, chat never shows one
_ACTION_PHRASES = {
    "mail": "sending an email",
    "gmail": "sending an email",
    "telegram": "sending a message",
    "discord": "sending a message",
    "slack": "sending a message",
    "whatsapp": "sending a message",
    "calendar": "a calendar action",
    "googlecalendar": "a calendar action",
    "github": "a github action",
    "notion": "a notion update",
    "linear": "a linear action",
    "payments": "a payment",
}

_NATIVE_PHRASES = {
    "send_chat_message": "messaging you",
    "memory_search": "searching my memory",
    "note_entity": "keeping a note",
    "get_pending_approvals": "checking pending approvals",
    "schedule_action": "scheduling an action",
    "request_screen_capture": "asking for a screenshot",
    "create_routine": "arming a routine",
    "list_routines": "listing routines",
    "set_routine_enabled": "pausing or re-arming a routine",
    "delete_routine": "deleting a routine",
    "explain_decision": "replaying a decision",
    "verify_integrity": "verifying my records",
    "get_config": "reading my configuration",
    "set_config": "changing my configuration",
    "workspace_read": "checking my workspace notes",
    "workspace_search": "searching my workspace notes",
    "workspace_remember": "writing a workspace note",
    "workspace_promote": "promoting a workspace note",
    "workspace_rewrite": "updating my identity or personality file",
    "workspace_finish_bootstrap": "finishing first-run setup",
}


def _plain_action(name: str) -> str:
    """Plain words for a tool call, the way the user would say it."""
    server, sep, tool = name.partition("__")
    if sep:
        lowered = tool.lower()
        # a composio tool carries its toolkit as the name's prefix —
        # composio__GMAIL_SEND_EMAIL reads as gmail, never as "composio"
        app = lowered.split("_", 1)[0] if server == "composio" else server
        # a read-like tool must never read as a send — "I went ahead with
        # sending an email" about listing mail would be a lie
        read_like = ("list", "search", "get", "read", "find", "check", "query", "fetch")
        if any(lowered.startswith(verb) for verb in read_like):
            return f"checking {app}"
        return _ACTION_PHRASES.get(app, f"an action in {app}")
    return _NATIVE_PHRASES.get(name, "an internal step")


def format_trace(t: Any) -> str:
    """Render one recorded trace for the model to answer from: the
    trigger, every gated call with its ruling, and what came back."""
    p = t.payload or {}
    lines = [f"Trace #{t.id} ({t.kind}) — {t.label or '(no label)'}"]
    trigger = p.get("trigger") or {}
    for m in trigger.get("messages") or []:
        lines.append(
            f"  asked: {m.get('handle', '?')} via {m.get('surface', '?')}: "
            f"{m.get('text', '')}"
        )
    observations = trigger.get("observations") or []
    if observations:
        lines.append(f"  seen: {len(observations)} new event(s)")
        for o in observations:
            lines.append(f"    [{o.get('source')}/{o.get('kind')}] {o.get('line', '')}")
    routine = p.get("routine")
    if routine:
        lines.append(
            f"  routine: #{routine.get('id')} '{routine.get('label')}' "
            f"when {json.dumps(routine.get('trigger') or {}, ensure_ascii=False)}"
        )
    event = p.get("event")
    if event:
        lines.append(
            f"  fired on: [{event.get('source')}/{event.get('kind')}] "
            f"{event.get('line', '')}"
        )
    action = p.get("action")
    if action:
        lines.append(
            f"  scheduled: #{action.get('id')} '{action.get('label')}' "
            f"at {action.get('run_at')}"
        )
    if p.get("approval_id") is not None:
        lines.append(
            f"  approval #{p.get('approval_id')}, decided by "
            f"{p.get('decided_by') or 'the user'}"
        )
    for c in p.get("calls") or []:
        gate = f"gate: {c.get('decision', '?')}"
        if c.get("matched_rule"):
            gate += f" ({c.get('matched_rule')})"
        if c.get("audit_seq") is not None:
            gate += f" [audit #{c.get('audit_seq')}]"
        if c.get("approval_id") is not None:
            gate += f" [held as approval #{c.get('approval_id')}]"
        lines.append(
            f"  → {c.get('name', '?')} "
            f"{json.dumps(c.get('params') or {}, ensure_ascii=False, default=str)} "
            f"— {gate} → {_clip(str(c.get('result', '')), 200)}"
        )
    if p.get("audit_seq") is not None and not p.get("calls"):
        lines.append(f"  audit: #{p.get('audit_seq')}")
    for r in p.get("reasoning") or []:
        lines.append(f"  reasoning: {r}")
    if p.get("reply"):
        lines.append(f"  reply: {p['reply']}")
    # a top-level result only exists on acts that are not a call list
    # (an approved call carried out); a scheduled fire's result already
    # shows on its call line
    if p.get("result") is not None and not p.get("calls"):
        suffix = " (error)" if p.get("is_error") else ""
        lines.append(f"  result: {_clip(str(p.get('result')), 200)}{suffix}")
    if p.get("error"):
        lines.append(f"  error: the turn failed — {p['error']}")
    return "\n".join(lines)


def plain_replay(trace: Any) -> str:
    """The recorded trace as the user reads it in chat: what happened, in
    plain words, with the trace id for everything exact. Internal call
    names and raw error text stay in the record — this never echoes them."""
    p = trace.payload or {}
    lines = [f"🧵 that message, from the record (trace #{trace.id}):"]

    trigger = p.get("trigger") or {}
    messages = trigger.get("messages") or []
    if messages:
        first = _clip(str(messages[0].get("text", "")), 200)
        more = f" (+{len(messages) - 1} more)" if len(messages) > 1 else ""
        lines.append(f"You asked: \"{first}\"{more}")
    elif p.get("routine"):
        lines.append(f"Your routine '{p['routine'].get('label')}' fired.")
    elif p.get("action"):
        lines.append(f"The scheduled action '{p['action'].get('label')}' came due.")
    elif p.get("approval_id") is not None:
        lines.append(
            f"Approval #{p.get('approval_id')}, decided by {p.get('decided_by') or 'you'}."
        )
    elif trigger.get("observations"):
        count = len(trigger["observations"])
        lines.append(f"I noticed {count} new event{'s' if count != 1 else ''}.")

    for c in p.get("calls") or []:
        action = _plain_action(str(c.get("name", "")))
        decision = str(c.get("decision", ""))
        if c.get("is_error") and decision != "deny":
            # the exact failure stays in the trace — chat gets the shape
            lines.append(f"I tried {action} — it didn't work out.")
        elif decision == "allow":
            rule = f" ({c.get('matched_rule')})" if c.get("matched_rule") else ""
            lines.append(f"I went ahead with {action} — the gate let it through{rule}.")
        elif decision == "require_approval":
            held = f" (#{c.get('approval_id')})" if c.get("approval_id") is not None else ""
            lines.append(f"I proposed {action}; the gate held it for your one-tap approval{held}.")
        elif decision == "deny":
            reason = f" ({c.get('reason')})" if c.get("reason") else ""
            lines.append(f"I proposed {action}; the gate blocked it{reason}.")
        else:
            lines.append(f"I proposed {action}; it didn't run.")

    # a top-level result only exists on acts that are not a call list
    # (an approved call carried out); a scheduled fire's result already
    # shows on its call line
    if p.get("result") is not None and not p.get("calls"):
        if p.get("is_error"):
            lines.append("It couldn't run — I said so at the time.")
        else:
            lines.append("It ran as approved.")

    if p.get("reply"):
        lines.append(f"I replied: \"{_clip(str(p['reply']), 200)}\"")

    lines.append(
        "The full recorded detail — the exact call, the parameters, the "
        f"gate's ruling — is trace #{trace.id} in the panel."
    )
    return "\n".join(lines)


def register_native_tools(
    *,
    registry: ToolRegistry,
    events: Any,
    entities: Any,
    approvals: Any,
    scheduler: Any,
    surfaces: Any,
    capture_box: Any,
    routines: Any = None,
    traces: Any = None,
    audit: Any = None,
    config_manager: Any = None,
    workspace: Any = None,
) -> int:
    """Add Aether's own tools to the flat namespace. Returns how many.

    `routines` wires the standing-trigger tools, `traces` the
    decision-replay tool, `audit` the integrity check, `config_manager` the
    configuration tools, and `workspace` the Markdown workspace tools;
    without the store behind one it is not offered at all (same shape as a
    loop without an MCP host: no feature, no dead tool spec)."""

    async def memory_search(params: dict[str, Any]) -> str:
        query = str(params.get("query", "")).strip()
        if not query:
            return "memory_search needs a query."
        found = await events.search(query, limit=int(params.get("limit", 5)))
        if not found:
            return "No matching memories."
        lines = []
        for e in found:
            lines.append(
                f"[{e.id}] {e.occurred_at:%Y-%m-%d %H:%M} {e.source}/{e.kind}: "
                + _clip(json.dumps(e.payload, default=str, ensure_ascii=False))
            )
        return "\n".join(lines)

    async def note_entity(params: dict[str, Any]) -> str:
        handle = str(params.get("handle", "")).strip()
        note = str(params.get("note", "")).strip()
        if not handle or not note:
            return "note_entity needs a handle and a note."
        platform = str(params.get("platform", "*")).strip() or "*"
        entity = await entities.resolve(platform, handle, str(params.get("display_name", "")))
        await entities.note(
            entity.id,
            {
                "kind": str(params.get("kind", "general")),
                "note": note,
                "handle": handle,
                "platform": platform,
            },
        )
        return f"Noted on {entity.display_name or handle} (identity {entity.id})."

    async def get_pending_approvals(params: dict[str, Any]) -> str:
        pending = await approvals.list_pending()
        if not pending:
            return "No pending approvals."
        return "\n".join(
            f"#{a.id} {a.tool_name} ({a.created_at:%Y-%m-%d %H:%M}, "
            f"expires {a.expires_at:%Y-%m-%d %H:%M}): "
            + _clip(json.dumps(a.params, default=str, ensure_ascii=False), 200)
            for a in pending
        )

    async def schedule_action(params: dict[str, Any]) -> str:
        label = str(params.get("label", "")).strip()
        tool_name = str(params.get("tool_name", "")).strip()
        if not label or not tool_name:
            return "schedule_action needs a label and a tool_name."
        raw_when = str(params.get("run_at", "")).strip()
        try:
            run_at = datetime.fromisoformat(raw_when)
        except ValueError:
            return f"run_at {raw_when!r} is not an ISO datetime (e.g. 2026-09-25T18:30:00+05:30)."
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)
        if run_at <= datetime.now(UTC) + timedelta(seconds=5):
            return "run_at must be in the future."
        action = await scheduler.create(
            label=label,
            run_at=run_at,
            payload={
                "type": "tool",
                "tool": tool_name,
                "params": dict(params.get("params") or {}),
            },
        )
        return (
            f"Scheduled as action {action.id} for {run_at.isoformat()}. It will "
            "still pass the authorization gate when it fires — a risky tool "
            "will ask the user then."
        )

    async def request_screen_capture(params: dict[str, Any]) -> str:
        reason = str(params.get("reason", "")).strip()
        capture_box.set(reason)
        return (
            "Screen capture requested — the companion (`aether_snap --poll`) "
            "picks the request up and uploads a screenshot; it will show up "
            "as a screen_capture memory as soon as it lands."
        )

    async def send_chat_message(params: dict[str, Any]) -> str:
        text = str(params.get("text", "")).strip()
        if not text:
            return "send_chat_message needs text."
        await surfaces.send_to_user(text)
        return "Sent to the user's chat surfaces."

    async def verify_integrity(params: dict[str, Any]) -> str:
        verification = await audit.verify_chain()
        newest = None
        if verification.entries:
            rows = await audit.recent(1)
            newest = rows[0]["created_at"] if rows else None
        return verification_text(verification, newest)

    async def get_config(params: dict[str, Any]) -> str:
        path = str(params.get("path", "")).strip() or None
        return await config_manager.show(path)

    async def set_config(params: dict[str, Any]) -> str:
        op = str(params.get("op", "")).strip().lower()
        path = str(params.get("path", "")).strip()
        if op not in ("set", "add", "remove", "reset"):
            return "set_config needs an op: set, add, remove, or reset."
        if not path:
            return "set_config needs a config path — e.g. agent.tick_seconds."
        if op == "set" and "value" not in params:
            return 'set needs a value — pass "value": null to clear one.'
        return await config_manager.set(
            op=op, path=path, value=params.get("value"), source="tool"
        )

    async def workspace_read(params: dict[str, Any]) -> str:
        file = str(params.get("file", "")).strip().lower()
        if not file:
            return "workspace_read needs a file: identity, soul, agents, user, memory, bootstrap, or daily."
        date_str = str(params.get("date", "")).strip() or None
        try:
            text = workspace.read(file, date_str=date_str)
        except ValueError as exc:
            return str(exc)
        return text or f"{file} is empty."

    async def workspace_search(params: dict[str, Any]) -> str:
        query = str(params.get("query", "")).strip()
        if not query:
            return "workspace_search needs a query."
        hits = workspace.search(query, limit=int(params.get("limit", 5)))
        if not hits:
            return "No matching workspace memory."
        return "\n".join(f"[{h.file}#{h.id or '-'}] {h.text}" for h in hits)

    async def workspace_remember(params: dict[str, Any]) -> str:
        text = str(params.get("text", "")).strip()
        kind = str(params.get("kind", "")).strip().lower()
        if not text or kind not in ("preference", "fact", "daily"):
            return "workspace_remember needs text and a kind: preference, fact, or daily."
        source = str(params.get("source") or "agent").strip().lower()
        try:
            entry_id = workspace.remember(
                text,
                kind,
                source=source,
                category=str(params.get("category") or "").strip() or None,
                confidence=params.get("confidence"),
                supersedes=[str(s) for s in (params.get("supersedes") or [])] or None,
            )
        except (SecretRejected, ValueError) as exc:
            return f"not saved: {exc}"
        except WorkspaceWriteError as exc:
            return f"not saved — {exc}"
        return f"Saved to the workspace as entry {entry_id}."

    async def workspace_promote(params: dict[str, Any]) -> str:
        date_str = str(params.get("date", "")).strip()
        entry_id = str(params.get("entry_id", "")).strip()
        if not date_str or not entry_id:
            return "workspace_promote needs a date (YYYY-MM-DD) and an entry_id."
        try:
            ok = workspace.promote(
                date_str, entry_id, category=str(params.get("category") or "").strip() or None
            )
        except ValueError as exc:
            return str(exc)
        except WorkspaceWriteError as exc:
            return f"not promoted — {exc}"
        return f"Promoted {entry_id} into MEMORY.md." if ok else f"No active entry {entry_id} on {date_str}."

    async def workspace_rewrite(params: dict[str, Any]) -> str:
        file = str(params.get("file", "")).strip().lower()
        text = str(params.get("text", "")).strip()
        if file not in ("identity", "soul"):
            return (
                "workspace_rewrite can only replace identity or soul whole — "
                "use workspace_remember for a preference or fact, and ask the "
                "user to edit agents/user/memory directly for anything else."
            )
        if not text:
            return "workspace_rewrite needs text."
        try:
            workspace.write_raw(file, text)
        except SecretRejected as exc:
            return f"not saved: {exc}"
        except WorkspaceWriteError as exc:
            return f"not saved — {exc}"
        return f"{file}.md replaced."

    async def workspace_finish_bootstrap(params: dict[str, Any]) -> str:
        try:
            done = workspace.finish_bootstrap()
        except WorkspaceWriteError as exc:
            return f"not finished — {exc}"
        return "First-run setup marked complete." if done else "No first-run setup was pending."

    def _describe_trigger(trigger: dict[str, Any]) -> str:
        parts: list[str] = []
        if trigger.get("from"):
            parts.append(f"from {trigger['from']}")
        if trigger.get("source"):
            parts.append(f"source {trigger['source']}")
        if trigger.get("kind"):
            parts.append(f"kind {trigger['kind']}")
        if trigger.get("contains"):
            parts.append(f"containing {trigger['contains']!r}")
        return " and ".join(parts) or "any event"

    def _routine_id(params: dict[str, Any]) -> int | None:
        raw = str(params.get("routine_id", "")).strip()
        try:
            return int(raw)
        except ValueError:
            return None

    async def create_routine(params: dict[str, Any]) -> str:
        label = str(params.get("label", "")).strip()
        tool_name = str(params.get("tool_name", "")).strip()
        trigger = {
            key: str(value).strip()
            for key, value in (
                ("source", params.get("when_source")),
                ("kind", params.get("when_kind")),
                ("from", params.get("when_from")),
                ("contains", params.get("when_contains")),
            )
            if str(value or "").strip()
        }
        if not label:
            return "create_routine needs a label."
        if not tool_name:
            return "create_routine needs a tool_name — what to run when it triggers."
        if not trigger:
            return (
                "create_routine needs at least one condition: when_source, "
                "when_kind, when_from, or when_contains. A reaction to "
                "'every event' is not something the user would want."
            )
        try:
            cooldown = int(params.get("cooldown_seconds", 300))
        except (TypeError, ValueError):
            return "cooldown_seconds must be an integer (seconds between fires)."
        routine = await routines.create(
            label=label,
            trigger=trigger,
            action={
                "type": "tool",
                "tool": tool_name,
                "params": dict(params.get("params") or {}),
            },
            cooldown_seconds=cooldown,
        )
        return (
            f"Routine {routine.id} '{label}' armed: when {_describe_trigger(trigger)}, "
            f"run {tool_name}. It still passes the authorization gate every "
            "time it fires — a risky tool will ask the user then."
        )

    async def list_routines(params: dict[str, Any]) -> str:
        rows = await routines.list()
        if not rows:
            return "No routines armed."
        lines = []
        for r in rows:
            fired = (
                f"fired {r.fire_count}x"
                + (f", last {r.last_fired_at:%Y-%m-%d %H:%M}" if r.last_fired_at else "")
                if r.fire_count
                else "never fired"
            )
            state = "paused" if not r.enabled else fired
            lines.append(
                f"#{r.id} '{r.label}' — when {_describe_trigger(r.trigger)} → "
                f"run {r.action.get('tool', '?')} ({state})"
            )
        return "\n".join(lines)

    async def set_routine_enabled(params: dict[str, Any]) -> str:
        routine_id = _routine_id(params)
        if routine_id is None:
            return "set_routine_enabled needs a routine_id."
        enabled = bool(params.get("enabled", True))
        routine = await routines.set_enabled(routine_id, enabled)
        if routine is None:
            return f"No routine {routine_id}."
        verb = "re-armed" if enabled else "paused"
        return f"Routine {routine_id} '{routine.label}' {verb}."

    async def delete_routine(params: dict[str, Any]) -> str:
        routine_id = _routine_id(params)
        if routine_id is None:
            return "delete_routine needs a routine_id."
        if await routines.delete(routine_id):
            return f"Routine {routine_id} deleted."
        return f"No routine {routine_id}."

    async def explain_decision(params: dict[str, Any]) -> str:
        raw_trace = str(params.get("trace_id", "")).strip()
        raw_approval = str(params.get("approval_id", "")).strip()
        about = str(params.get("about", "")).strip()
        try:
            limit = max(1, int(params.get("limit", 3)))
        except (TypeError, ValueError):
            limit = 3

        if raw_trace:
            try:
                trace_id = int(raw_trace)
            except ValueError:
                return f"trace_id {raw_trace!r} is not a number."
            trace = await traces.get(trace_id)
            if trace is None:
                return f"No recorded trace {trace_id}."
            return format_trace(trace)

        if raw_approval:
            try:
                approval_id = int(raw_approval)
            except ValueError:
                return f"approval_id {raw_approval!r} is not a number."
            origins: list[Any] = []  # the act that proposed the held call
            carries: list[Any] = []  # the approved call running
            for t in await traces.recent(50):
                p = t.payload or {}
                if p.get("approval_id") == approval_id:
                    carries.append(t)
                elif any(
                    c.get("approval_id") == approval_id for c in p.get("calls") or []
                ):
                    origins.append(t)
            if not origins and not carries:
                return f"No recorded trace involving approval #{approval_id}."
            return "\n\n".join(format_trace(t) for t in (origins + carries)[:4])

        if about:
            needle = about.lower()
            found = [
                t
                for t in await traces.recent(50)
                if needle
                in " ".join(
                    [t.kind, t.label or "", json.dumps(t.payload or {}, default=str, ensure_ascii=False)]
                ).lower()
            ]
            if not found:
                return f"No recorded trace mentioning {about!r}."
            return "\n\n".join(format_trace(t) for t in found[:limit])

        rows = await traces.recent(limit)
        if not rows:
            return "No decision traces recorded yet."
        return "\n\n".join(format_trace(t) for t in rows)

    natives: list[tuple[ToolSpec, NativeHandler]] = [
        (
            _spec(
                "memory_search",
                "Search Aether's cross-app memory for past events, messages, "
                "screen captures, and notes.",
                {
                    "query": {"type": "string", "description": "what to look for"},
                    "limit": {"type": "integer", "description": "max results (default 5)"},
                },
                ["query"],
            ),
            memory_search,
        ),
        (
            _spec(
                "note_entity",
                "Keep a relationship note about a person in Aether's memory "
                "(e.g. 'owes me a reply', 'book club Fridays').",
                {
                    "handle": {"type": "string", "description": "email, @handle, or phone"},
                    "note": {"type": "string", "description": "what to remember"},
                    "platform": {"type": "string", "description": "platform hint, * for any"},
                    "display_name": {"type": "string", "description": "name, if known"},
                    "kind": {"type": "string", "description": "note category"},
                },
                ["handle", "note"],
            ),
            note_entity,
        ),
        (
            _spec(
                "get_pending_approvals",
                "List tool calls parked for the user's one-tap approval.",
                {},
                [],
            ),
            get_pending_approvals,
        ),
        (
            _spec(
                "schedule_action",
                "Run a tool call at a future time. Persisted — it survives "
                "restarts. The call still passes the authorization gate at "
                "fire time.",
                {
                    "label": {"type": "string", "description": "short human label"},
                    "run_at": {"type": "string", "description": "ISO datetime with timezone"},
                    "tool_name": {"type": "string", "description": "tool to run"},
                    "params": {"type": "object", "description": "arguments for the tool"},
                },
                ["label", "run_at", "tool_name"],
            ),
            schedule_action,
        ),
        (
            _spec(
                "request_screen_capture",
                "Ask the user's companion for a fresh screenshot (it becomes "
                "a screen_capture memory — perception only).",
                {"reason": {"type": "string", "description": "why you want a look"}},
                [],
            ),
            request_screen_capture,
        ),
        (
            _spec(
                "send_chat_message",
                "Push a message to the user's chat surfaces mid-turn "
                "(Telegram, Discord, Slack, web chat — whichever are "
                "enabled). Your end-of-turn reply is delivered "
                "automatically — never use this to answer or to repeat it.",
                {"text": {"type": "string", "description": "the message"}},
                ["text"],
            ),
            send_chat_message,
        ),
    ]
    if routines is not None:
        natives.extend(
            [
                (
                    _spec(
                        "create_routine",
                        "Arm a standing reaction: 'when X happens, run Y'. "
                        "Conditions (when_source, when_kind, when_from, "
                        "when_contains) are AND-combined; at least one is "
                        "required. Every fire passes the authorization gate.",
                        {
                            "label": {"type": "string", "description": "short human label"},
                            "when_source": {"type": "string", "description": "event source to match, e.g. mail"},
                            "when_kind": {"type": "string", "description": "event kind, e.g. chat_message"},
                            "when_from": {"type": "string", "description": "sender handle to match"},
                            "when_contains": {"type": "string", "description": "text the event must contain"},
                            "tool_name": {"type": "string", "description": "tool to run on a match"},
                            "params": {"type": "object", "description": "arguments for that tool"},
                            "cooldown_seconds": {
                                "type": "integer",
                                "description": "minimum seconds between fires (default 300)",
                            },
                        },
                        ["label", "tool_name"],
                    ),
                    create_routine,
                ),
                (
                    _spec(
                        "list_routines",
                        "List the user's armed routines — trigger, action, "
                        "and how often each has fired.",
                        {},
                        [],
                    ),
                    list_routines,
                ),
                (
                    _spec(
                        "set_routine_enabled",
                        "Pause or re-arm a routine without deleting it.",
                        {
                            "routine_id": {"type": "integer", "description": "routine to toggle"},
                            "enabled": {"type": "boolean", "description": "false pauses it"},
                        },
                        ["routine_id"],
                    ),
                    set_routine_enabled,
                ),
                (
                    _spec(
                        "delete_routine",
                        "Delete one of the user's routines.",
                        {"routine_id": {"type": "integer", "description": "routine to delete"}},
                        ["routine_id"],
                    ),
                    delete_routine,
                ),
            ]
        )
    if traces is not None:
        natives.extend(
            [
                (
                    _spec(
                        "explain_decision",
                        "Replay why an action happened: the recorded trace of "
                        "what was seen, what was proposed, how the "
                        "authorization gate ruled, and what came back. Look it "
                        "up by trace_id, by the approval_id a call was held as, "
                        "or by a topic (about); with nothing given, the most "
                        "recent traces. Answer 'why did you do that?' from the "
                        "record, not from memory.",
                        {
                            "trace_id": {"type": "integer", "description": "a specific trace"},
                            "approval_id": {
                                "type": "integer",
                                "description": "the approval a call was held as",
                            },
                            "about": {
                                "type": "string",
                                "description": "topic to search recent traces for",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "max traces when listing (default 3)",
                            },
                        },
                        [],
                    ),
                    explain_decision,
                ),
            ]
        )
    if audit is not None:
        natives.extend(
            [
                (
                    _spec(
                        "verify_integrity",
                        "Verify the tamper-evident hash chain of the decision "
                        "record and answer in plain words. Use it whenever the "
                        "user asks whether your records can be trusted — 'can "
                        "anyone tamper with your logs?', 'prove nothing was "
                        "edited'.",
                        {},
                        [],
                    ),
                    verify_integrity,
                ),
            ]
        )
    if config_manager is not None:
        natives.extend(
            [
                (
                    _spec(
                        "get_config",
                        "Read Aether's current configuration in plain words: "
                        "the overview (with everything changed from "
                        "config.yaml marked), one section ('llm', 'agent', "
                        "'salience', 'messaging', 'contacts', 'authz', "
                        "'mcp_servers'), or one setting by its dotted path. "
                        "Use it whenever the user asks what's set, what a "
                        "setting is, or what they've changed from chat.",
                        {
                            "path": {
                                "type": "string",
                                "description": "optional section or dotted path, e.g. agent.tick_seconds",
                            },
                        },
                        [],
                    ),
                    get_config,
                ),
                (
                    _spec(
                        "set_config",
                        "Change a config.yaml setting from chat. op: set (a "
                        "value, or null to clear), add/remove (list entries), "
                        "reset (back to config.yaml). Paths: "
                        "agent.tick_seconds, agent.max_tool_iterations, "
                        "agent.daily_surface_cap, agent.quiet_hours, "
                        "agent.quiet_urgent_salience, salience.threshold, "
                        "salience.rate_cap_per_hour, llm.provider, llm.model, "
                        "llm.vision_model, llm.salience_model, llm.max_tokens, "
                        "llm.ollama_vision, messaging.<platform>.enabled, "
                        "contacts.mode, contacts.allowlist, authz.rules, "
                        "authz.approval_ttl_hours, mcp_servers, "
                        "mcp_servers.<name>.enabled. Tuning (agent.*, "
                        "salience.*) applies immediately; llm.* persists and "
                        "Aether restarts itself to load it; security sections "
                        "(contacts, authz, messaging, mcp_servers) are held "
                        "for the user's one-tap approval — when a call is "
                        "held, say so plainly and do not propose it again. "
                        "Secrets (.env) are not part of this vocabulary.",
                        {
                            "op": {
                                "type": "string",
                                "description": "set, add, remove, or reset",
                            },
                            "path": {
                                "type": "string",
                                "description": "the dotted setting path",
                            },
                            "value": {
                                "description": "the new value, or null to clear — "
                                "for add/remove the list entry, e.g. "
                                '{"platform": "telegram", "handle": "@friend"} '
                                "for contacts.allowlist",
                            },
                        },
                        ["op", "path"],
                    ),
                    set_config,
                ),
            ]
        )
    if workspace is not None:
        natives.extend(
            [
                (
                    _spec(
                        "workspace_read",
                        "Read one file from Aether's human-editable Markdown "
                        "workspace: identity, soul, agents, user, memory, "
                        "bootstrap (only present during first-run setup), or "
                        "daily (today's working notes, or an older date).",
                        {
                            "file": {
                                "type": "string",
                                "description": "identity | soul | agents | user | memory | bootstrap | daily",
                            },
                            "date": {
                                "type": "string",
                                "description": "YYYY-MM-DD, for file: daily — defaults to today",
                            },
                        },
                        ["file"],
                    ),
                    workspace_read,
                ),
                (
                    _spec(
                        "workspace_search",
                        "Search the whole workspace (identity, soul, agent "
                        "instructions, user preferences, long-term memory, "
                        "and recent daily notes) for a keyword or phrase.",
                        {
                            "query": {"type": "string", "description": "what to look for"},
                            "limit": {"type": "integer", "description": "max results (default 5)"},
                        },
                        ["query"],
                    ),
                    workspace_search,
                ),
                (
                    _spec(
                        "workspace_remember",
                        "Write a durable note to the workspace — sparingly; "
                        "most things belong in ordinary memory_search memory, "
                        "not here. kind=preference (USER.md, something the "
                        "user consistently wants — pass supersedes with any "
                        "entry ids it replaces), kind=fact (MEMORY.md, a "
                        "durable fact or decision; category groups related "
                        "facts, default 'Notes'), or kind=daily (today's "
                        "working notes, for context worth keeping only for "
                        "this session). Refuses anything that looks like a "
                        "secret, password, or API key.",
                        {
                            "text": {"type": "string", "description": "what to remember"},
                            "kind": {"type": "string", "description": "preference | fact | daily"},
                            "source": {
                                "type": "string",
                                "description": "user (they stated this directly) or agent "
                                "(you inferred it) — default agent",
                            },
                            "category": {"type": "string", "description": "fact only: section heading in MEMORY.md"},
                            "confidence": {"type": "number", "description": "0-1, if this is an inference rather than something stated"},
                            "supersedes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "preference only: entry ids this one replaces",
                            },
                        },
                        ["text", "kind"],
                    ),
                    workspace_remember,
                ),
                (
                    _spec(
                        "workspace_rewrite",
                        "Replace IDENTITY.md or SOUL.md wholesale — only "
                        "when the user explicitly asks you to change who "
                        "you are or how you sound. For anything else "
                        "(preferences, facts, daily notes) use "
                        "workspace_remember instead.",
                        {
                            "file": {"type": "string", "description": "identity | soul"},
                            "text": {"type": "string", "description": "the file's full new content"},
                        },
                        ["file", "text"],
                    ),
                    workspace_rewrite,
                ),
                (
                    _spec(
                        "workspace_promote",
                        "Promote an entry from a daily working-notes file "
                        "into MEMORY.md, because it turned out to matter "
                        "beyond that day. The original stays on record, "
                        "marked promoted.",
                        {
                            "date": {"type": "string", "description": "YYYY-MM-DD the entry was written on"},
                            "entry_id": {"type": "string", "description": "the entry's id, from workspace_search or workspace_read"},
                            "category": {"type": "string", "description": "section heading in MEMORY.md, default 'Notes'"},
                        },
                        ["date", "entry_id"],
                    ),
                    workspace_promote,
                ),
                (
                    _spec(
                        "workspace_finish_bootstrap",
                        "Mark first-run workspace setup complete, once "
                        "you've introduced yourself and learned the user's "
                        "basic preferences. Removes BOOTSTRAP.md.",
                        {},
                        [],
                    ),
                    workspace_finish_bootstrap,
                ),
            ]
        )
    for spec, handler in natives:
        registry.add_native(spec, handler)
    return len(natives)
