"""Typed turn outcomes and the notification policy.

Every turn ends in one of four values:

- ``Reply(text, internal_notes)`` — an answer to a user message. Only
  ``text`` is delivered; ``internal_notes`` is sealed in the trace.
- ``Notify(content)`` — an unprompted note. Offered only when code says the
  user may be notified now.
- ``Ask(question, options)`` — a question to the user.
- ``NoneOutcome()`` — nothing leaves the agent.

The decision is structural: the model can only reach the user through a
terminal tool whose input schema is one of these shapes. Free text on an
unprompted turn is internal by definition, so it can never become a message
that explains why no message was sent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..llm.types import ToolCall, ToolSpec
from ..memory.timewindow import in_quiet_window
from ..message_catalog import render

REPLY_TOOL = "reply_user"
NOTIFY_TOOL = "notify_user"
ASK_TOOL = "ask_user"

TERMINAL_TOOLS = frozenset({REPLY_TOOL, NOTIFY_TOOL, ASK_TOOL})


@dataclass(frozen=True)
class Reply:
    text: str
    internal_notes: str = ""


@dataclass(frozen=True)
class Notify:
    content: str


@dataclass(frozen=True)
class Ask:
    question: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoneOutcome:
    reason: str = ""


Outcome = Reply | Notify | Ask | NoneOutcome


@dataclass(frozen=True)
class NotificationDecision:
    """Whether a proactive notify may be offered, and why."""

    allowed: bool
    reason: str = ""
    keys: tuple[str, ...] = field(default_factory=tuple)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def reply_spec() -> ToolSpec:
    return ToolSpec(
        name=REPLY_TOOL,
        description=(
            "Deliver your answer to the user. Put the words they should read in "
            "`reply`. Put every decision, reason, caveat, source id, finding id or "
            "verification detail in `internal_notes` — it is stored sealed and is "
            "never shown to the user or placed in later conversation memory."
        ),
        input_schema=_schema(
            {
                "reply": {
                    "type": "string",
                    "description": "The complete answer the user reads.",
                },
                "internal_notes": {
                    "type": "string",
                    "description": (
                        "Why you answered this way, what you decided, and anything "
                        "you could not verify. Never delivered."
                    ),
                },
            },
            ["reply"],
        ),
    )


def notify_spec() -> ToolSpec:
    return ToolSpec(
        name=NOTIFY_TOOL,
        description=(
            "Send one unprompted note to the user. Use it only when there is "
            "something they need to know now. If there is nothing worth saying, "
            "do not call this and do not explain the silence."
        ),
        input_schema=_schema(
            {
                "content": {
                    "type": "string",
                    "description": "The note the user reads.",
                },
            },
            ["content"],
        ),
    )


def ask_spec() -> ToolSpec:
    return ToolSpec(
        name=ASK_TOOL,
        description=(
            "Ask the user one question. `options` is an optional short list of "
            "answers they can choose from. Use this instead of delivering an "
            "answer when you need their decision."
        ),
        input_schema=_schema(
            {
                "question": {
                    "type": "string",
                    "description": "The question the user reads.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short answers, in the order to show them.",
                },
            },
            ["question"],
        ),
    )


def outcome_specs(*, solicited: bool, notify_allowed: bool) -> list[ToolSpec]:
    """The terminal tools this turn offers.

    A solicited turn always gets `reply_user` and `ask_user`. An unprompted
    turn gets `ask_user`, and gets `notify_user` only when the policy says
    the user may be notified now — when it says no, the action does not
    exist for the model to call.
    """
    specs: list[ToolSpec] = []
    if solicited:
        specs.append(reply_spec())
        specs.append(ask_spec())
    elif notify_allowed:
        # an unprompted turn may reach the person only when code says the
        # moment is right: both the note and the question are interruptions
        specs.append(notify_spec())
        specs.append(ask_spec())
    return specs


def is_terminal_tool(name: str) -> bool:
    return name in TERMINAL_TOOLS


def outcome_from_params(name: str, params: dict[str, Any]) -> Outcome | None:
    """Parse one terminal call's arguments into a typed outcome."""
    if name == REPLY_TOOL:
        text = str(params.get("reply") or "").strip()
        notes = str(params.get("internal_notes") or "").strip()
        return Reply(text=text, internal_notes=notes)
    if name == NOTIFY_TOOL:
        content = str(params.get("content") or "").strip()
        return Notify(content=content)
    if name == ASK_TOOL:
        question = str(params.get("question") or "").strip()
        raw = params.get("options")
        options: tuple[str, ...] = ()
        if isinstance(raw, list):
            options = tuple(str(item).strip() for item in raw if str(item).strip())
        return Ask(question=question, options=options)
    return None


def outcome_from_calls(calls: list[dict[str, Any]]) -> Outcome | None:
    """The last accepted terminal call in a turn's recorded calls."""
    for entry in reversed(calls):
        if not entry.get("terminal"):
            continue
        name = str(entry.get("name") or "")
        params = entry.get("params")
        if not isinstance(params, dict):
            continue
        parsed = outcome_from_params(name, params)
        if parsed is not None:
            return parsed
    return None


def render_ask(outcome: Ask, *, locale: str = "en") -> str:
    """The ask as the user reads it, with its options from the catalog."""
    if not outcome.options:
        return outcome.question
    return render(
        "ask.options",
        locale=locale,
        question=outcome.question,
        options=" · ".join(outcome.options),
    )


def _action_digest(tool: str, params: Any) -> str:
    payload = json.dumps(
        {"tool": tool, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def notification_keys(observations: list[Any]) -> tuple[str, ...]:
    """The event and action identities one proactive turn is about.

    A later turn that sees the same action (the same approval, or the same
    tool and parameters) carries the same action key, so "already reported"
    survives a retry or a re-ingested event.
    """
    keys: list[str] = []
    for event in observations:
        event_id = getattr(event, "id", None)
        source = getattr(event, "source", "")
        payload = getattr(event, "payload", None)
        action_keys: list[str] = []
        if source == "agent" and isinstance(payload, dict):
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, int) and not isinstance(approval_id, bool):
                action_keys.append(f"action:{approval_id}")
            tool = payload.get("tool")
            if isinstance(tool, str) and tool:
                action_keys.append(f"action:{_action_digest(tool, payload.get('params'))}")
        if action_keys:
            # the action is the identity: a retry that mints a new event row
            # for the same failure is still the same thing to report
            keys.extend(action_keys)
        elif isinstance(event_id, int) and event_id > 0:
            keys.append(f"event:{event_id}")
    return tuple(dict.fromkeys(keys))


def evaluate_notification_policy(
    config: AppConfig,
    *,
    quiet: tuple[int, int] | None,
    now: datetime,
    urgency: float,
    keys: tuple[str, ...],
    reported: set[str],
) -> NotificationDecision:
    """Code decides whether a proactive notify may be offered.

    The user's stored preferences are the `notifications` section; quiet
    hours come from `agent.quiet_hours` and its urgency break; "already
    reported" is the sealed report state filtered by the configured window.
    No model text participates in this decision.
    """
    rules = config.notifications
    if not rules.enabled:
        return NotificationDecision(False, "notifications are off", keys)
    if urgency < rules.min_urgency:
        return NotificationDecision(False, "below the notification urgency", keys)
    if in_quiet_window(quiet, now) and urgency < config.agent.quiet_urgent_salience:
        return NotificationDecision(False, "quiet hours", keys)
    if keys and set(keys).issubset(reported):
        return NotificationDecision(False, "already reported", keys)
    return NotificationDecision(True, "", keys)


def record_notification(
    calls: list[dict[str, Any]],
    call: ToolCall,
    *,
    accepted: bool,
    plain: str,
) -> None:
    """Append one terminal call to the turn trace, accepted or refused."""
    calls.append(
        {
            "id": call.id,
            "name": call.name,
            "plain": call.plain or plain,
            "params": dict(call.arguments),
            "decision": "allow" if accepted else "error",
            "matched_rule": "builtin:outcome",
            "reason": "" if accepted else "not offered by policy",
            "audit_seq": None,
            "approval_id": None,
            "result": "outcome accepted" if accepted else "not offered by policy",
            "is_error": not accepted,
            "terminal": accepted,
        }
    )
