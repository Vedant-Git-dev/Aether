"""Typed turn outcomes and the notification policy.

Every turn ends in one of four values:

- ``Reply(text, internal_notes)`` — an answer to a user message. Only
  ``text`` is delivered; ``internal_notes`` is sealed in the trace.
- ``Notify(content)`` — an unprompted note. Offered only when code says the
  user may be notified now.
- ``Ask(question, options)`` — a question to the user.
- ``NoneOutcome()`` — nothing leaves the agent.

The model sees one ``message__send`` tool and one ``message__ask`` tool. The
turn type decides whether a send becomes a Reply or a Notify; the model never
chooses the delivery mode, so there is no second delivery tool to bypass the
policy.

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
from ..llm.types import ToolCall, WireSpec, with_plain_param
from ..memory.timewindow import in_quiet_window
from ..message_catalog import render

SEND_TOOL = "message__send"
ASK_TOOL = "message__ask"

# Old traces, stored approvals and scripted turns still carry the first names.
# They all resolve to the one send outcome; whether that send is a reply or a
# proactive note is decided by the turn type in code, never by the model.
LEGACY_TERMINAL = {
    "message__reply": SEND_TOOL,
    "message__notify": SEND_TOOL,
    "message.reply": SEND_TOOL,
    "message.notify": SEND_TOOL,
    "reply_user": SEND_TOOL,
    "notify_user": SEND_TOOL,
    "message.ask": ASK_TOOL,
    "ask_user": ASK_TOOL,
}
TERMINAL_TOOLS = frozenset({SEND_TOOL, ASK_TOOL, *LEGACY_TERMINAL})

# one line each for the prompt index — the same shape every other tool has
OUTCOME_SUMMARIES = {
    SEND_TOOL: "Send the user the words they should read.",
    ASK_TOOL: "Ask the user one question and wait for their answer.",
}


def canonical_tool(name: str) -> str:
    return LEGACY_TERMINAL.get(name, name)


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
    return with_plain_param({"type": "object", "properties": properties, "required": required})


def send_spec() -> WireSpec:
    return WireSpec(
        name=SEND_TOOL,
        description=(
            "Deliver the words the user reads. On a turn where they spoke first "
            "this is your answer; on an unprompted turn it is a proactive note. "
            "The turn type decides which — there is no separate reply or notify "
            "tool. Put every decision, reason, caveat, source id, finding id or "
            "verification detail in `internal_notes`: it is stored sealed and is "
            "never shown to the user or placed in later conversation memory."
        ),
        input_schema=_schema(
            {
                "text": {
                    "type": "string",
                    "description": "The complete message the user reads.",
                },
                "internal_notes": {
                    "type": "string",
                    "description": (
                        "Why you sent this, what you decided, and anything you "
                        "could not verify. Never delivered."
                    ),
                },
            },
            ["text"],
        ),
    )


def ask_spec() -> WireSpec:
    return WireSpec(
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


def outcome_specs(*, solicited: bool, notify_allowed: bool) -> list[WireSpec]:
    """The terminal tools this turn offers.

    The model always sees the same two tools: one send and one ask. Whether a
    send is delivered as a reply or as a proactive note is decided by the turn
    type in code. An unprompted turn gets neither when the notification policy
    says the person may not be interrupted now.
    """
    if solicited or notify_allowed:
        return [send_spec(), ask_spec()]
    return []


def is_terminal_tool(name: str) -> bool:
    return canonical_tool(name) in (SEND_TOOL, ASK_TOOL)


def outcome_from_params(
    name: str, params: dict[str, Any], *, solicited: bool = True
) -> Outcome | None:
    """Parse one terminal call's arguments into a typed outcome.

    A send is a Reply when the user spoke first and a Notify on an unprompted
    turn. The model never chooses the delivery mode.
    """
    name = canonical_tool(name)
    if name == SEND_TOOL:
        # `text` is the wire field; `content` and `reply` keep old scripted
        # turns, stored calls and tests resolving without a migration
        text = str(params.get("text") or params.get("content") or params.get("reply") or "").strip()
        notes = str(params.get("internal_notes") or "").strip()
        if solicited:
            return Reply(text=text, internal_notes=notes)
        return Notify(content=text)
    if name == ASK_TOOL:
        question = str(params.get("question") or "").strip()
        raw = params.get("options")
        options: tuple[str, ...] = ()
        if isinstance(raw, list):
            options = tuple(str(item).strip() for item in raw if str(item).strip())
        return Ask(question=question, options=options)
    return None


def outcome_from_calls(calls: list[dict[str, Any]], *, solicited: bool = True) -> Outcome | None:
    """The last accepted terminal call in a turn's recorded calls."""
    for entry in reversed(calls):
        if not entry.get("terminal"):
            continue
        name = str(entry.get("name") or "")
        params = entry.get("params")
        if not isinstance(params, dict):
            continue
        parsed = outcome_from_params(name, params, solicited=solicited)
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
