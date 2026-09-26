"""Aether's native tools — reading its own memory, keeping entity notes,
scheduling, requesting screen captures, talking to the user, and managing
the user's routines.

These are internal by construction: they touch only Aether's own state
(or the owner's own chat surfaces), which is why the authz classifier's
`builtin:internal` rule lets them run without approval. Everything that
leaves Aether still goes through the gate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..connectors.registry import ToolRegistry
from ..llm.types import ToolSpec

log = logging.getLogger("aether.agent.tools")

# what the tools need, expressed as protocols the loop actually owns
EventSearch = Callable[..., Awaitable[Any]]
NativeHandler = Callable[[dict[str, Any]], Awaitable[str]]


def _spec(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> ToolSpec:
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
) -> int:
    """Add Aether's own tools to the flat namespace. Returns how many.

    `routines` wires the standing-trigger tools; without a store they are
    not offered at all (same shape as a loop without an MCP host: no
    feature, no dead tool spec)."""

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
        entity = await entities.resolve(
            platform, handle, str(params.get("display_name", ""))
        )
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
            return (
                f"run_at {raw_when!r} is not an ISO datetime "
                "(e.g. 2026-09-25T18:30:00+05:30)."
            )
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        if run_at <= datetime.now(timezone.utc) + timedelta(seconds=5):
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
                "Send a message to the user's chat surfaces (Telegram, "
                "Discord, Slack, web chat — whichever are enabled).",
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
    for spec, handler in natives:
        registry.add_native(spec, handler)
    return len(natives)
