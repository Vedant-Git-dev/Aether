"""One alert's stored condition — sealed, deterministic, model-free.

An alert is either event-based (``kind=alert``: a source, optionally a sender,
an event kind or some words) or time-based (``kind=reminder``: a ``remind_at``
moment). Matching is pure code over one event; the model is nowhere in the
decision of whether an alert matches.

The row is legacy-shaped on purpose: watches armed before this model existed
carry ``description``, ``urgency_min``, ``needs_reply``, ``channel`` and
``quiet_hours``, so those fields are still read and still matched. The tool
schema never offers them again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..memory.enrich import enrichment_text
from ..memory.entities import normalize_handle
from ..memory.mail import MAIL_KIND, parse_addresses

ACTIVE = "active"
PAUSED = "paused"

ALERT = "alert"
REMINDER = "reminder"
KINDS = (ALERT, REMINDER)

IMMEDIATE = "immediate"
DIGEST = "digest"
STYLES = (IMMEDIATE, DIGEST)

_SENDER_KEY = "_sender"
_MAIL_SCOPE = "mail"
_CHAT_SCOPE = "chat"


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _bare(handle: str) -> str:
    value = (handle or "").strip().lower()
    if value.startswith("mailto:"):
        value = value[7:]
    return value.lstrip("@")


def _kind(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return REMINDER if clean == REMINDER else ALERT


def _style(value: Any, default: str = IMMEDIATE) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in STYLES else default


# Significant words are three letters or longer; the short function words a
# description carries ("the", "of", "an") never decide a match.
_WORD_RE = re.compile(r"[a-z0-9]+")
_WORD_MIN = 3


def _words(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall((text or "").lower()) if len(word) >= _WORD_MIN}


def text_overlap(description: str, text: str) -> bool:
    """Softer than an exact phrase: at least half of the description's
    significant words appear anywhere in the event. A one- or two-word
    description therefore needs one of them; a longer one needs about half.
    The scope and sender conditions still bound what can match."""
    wanted = _words(description)
    if not wanted:
        return False
    found = _words(text)
    needed = max(1, len(wanted) // 2)
    return len(wanted & found) >= needed


def _action(value: Any) -> dict[str, Any] | None:
    """A folded routine's action: one tool call, or nothing."""
    if not isinstance(value, dict):
        return None
    tool = _clean(value.get("tool") or value.get("tool_name"), 120)
    if not tool:
        return None
    params = value.get("params")
    return {"tool": tool, "params": dict(params) if isinstance(params, dict) else {}}


@dataclass(frozen=True)
class AlertSpec:
    """What one alert watches, and what (if anything) it runs.

    `scope` is a manifest key ("mail", "chat", ...). `event` narrows to one
    event kind, `sender` to one address or handle, `contains` to exact words.
    `style` chooses immediate wording or a digest. `remind_at` is the ISO
    moment of a reminder. `action` is an optional tool call a match runs
    through the ordinary authorization gate — the folded routine.
    """

    title: str = ""
    kind: str = ALERT
    scope: str = ""
    event: str = ""
    sender: str = ""
    contains: str = ""
    style: str = IMMEDIATE
    remind_at: str = ""
    action: dict[str, Any] | None = None
    # legacy watch fields, read and matched but no longer offered
    description: str = ""
    urgency_min: int = 0
    needs_reply: bool = False
    channel: str = ""
    quiet_hours: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "scope": self.scope,
            "event": self.event,
            "sender": self.sender,
            "contains": self.contains,
            "style": self.style,
            "remind_at": self.remind_at,
            "action": dict(self.action) if self.action else None,
            "description": self.description,
            "urgency_min": self.urgency_min,
            "needs_reply": self.needs_reply,
            "channel": self.channel,
            "quiet_hours": self.quiet_hours,
        }

    @classmethod
    def from_payload(cls, data: Any) -> AlertSpec:
        """Whatever shape an older writer left, coerced — a malformed field
        degrades to its default rather than taking the evaluator down."""
        if not isinstance(data, dict):
            return cls()
        try:
            urgency = int(data.get("urgency_min") or 0)
        except (TypeError, ValueError):
            urgency = 0
        kind = data.get("kind")
        # a watch stored its event kind in `kind` and its wording in `form`;
        # an old "reminder" was still event-based, so it stays an alert
        raw_kind = _clean(kind, 80)
        remind_at = _clean(data.get("remind_at"), 64)
        alert_kind = REMINDER if raw_kind == REMINDER and remind_at else ALERT
        event = _clean(data.get("event"), 80)
        if not event and raw_kind not in ("", "*", ALERT, REMINDER):
            event = raw_kind
        return cls(
            title=_clean(data.get("title") or data.get("label")),
            kind=alert_kind,
            scope=_clean(data.get("scope"), 40),
            event=event,
            sender=_clean(data.get("sender")),
            contains=_clean(data.get("contains"), 300),
            style=_style(data.get("style")),
            remind_at=remind_at,
            action=_action(data.get("action")),
            description=_clean(data.get("description"), 300),
            urgency_min=max(0, min(3, urgency)),
            needs_reply=bool(data.get("needs_reply", False)),
            channel=_clean(data.get("channel"), 40),
            quiet_hours=_clean(data.get("quiet_hours"), 40),
        )

    @property
    def is_reminder(self) -> bool:
        return self.kind == REMINDER

    def criteria(self) -> dict[str, Any]:
        """The fields that decide a match — titles and wording style do not."""
        return {
            "kind": self.kind,
            "scope": self.scope,
            "event": self.event,
            "sender": self.sender,
            "contains": self.contains.lower(),
            "remind_at": self.remind_at,
            "action": dict(self.action) if self.action else None,
        }

    def equivalent(self, other: AlertSpec) -> bool:
        return self.criteria() == other.criteria()


def normalize_spec(spec: AlertSpec) -> AlertSpec:
    """One spec with trimmed words, a clamped urgency and a valid style."""
    return AlertSpec(
        title=_clean(spec.title),
        kind=_kind(spec.kind),
        scope=_clean(spec.scope, 40).lower(),
        event=_clean(spec.event, 80),
        sender=_clean(spec.sender),
        contains=_clean(spec.contains, 300),
        style=_style(spec.style),
        remind_at=_clean(spec.remind_at, 64),
        action=_action(spec.action),
        description=_clean(spec.description, 300),
        urgency_min=max(0, min(3, int(spec.urgency_min))),
        needs_reply=bool(spec.needs_reply),
        channel=_clean(spec.channel, 40),
        quiet_hours=_clean(spec.quiet_hours, 40),
    )


def has_criteria(spec: AlertSpec) -> bool:
    """A reminder needs a moment; an alert needs at least one condition.

    A bare "tell me about anything" would fire on every event, so the tool
    refuses one — this is the one place that decides."""
    if spec.is_reminder:
        return bool(spec.remind_at)
    return bool(
        spec.scope
        or spec.event
        or spec.sender
        or spec.contains
        or spec.description
        or spec.urgency_min
        or spec.needs_reply
    )


def _scope_matches(scope: str, event: Any) -> bool:
    clean = (scope or "").strip().lower()
    if clean in ("", "*", "any"):
        return True
    if clean == _MAIL_SCOPE:
        return event.kind == MAIL_KIND
    if clean == _CHAT_SCOPE:
        return event.kind == "chat_message"
    return event.source.lower() == clean


def _address_matches(wanted: str, address: str, name: str) -> bool:
    clean = wanted.strip().lower()
    address = (address or "").strip().lower()
    name = (name or "").strip().lower()
    if not clean:
        return True
    return clean == address or _bare(clean) == _bare(address) or (bool(name) and clean in name)


def _sender_matches(pattern: str, event: Any) -> bool:
    """A sender condition, in both shapes an event carries one: the raw mail
    header a poll stored, and the encrypted `_sender` block a chat surface
    attaches."""
    wanted = (pattern or "").strip()
    if not wanted:
        return True
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.kind == MAIL_KIND:
        for address in parse_addresses(payload.get("sender")):
            if _address_matches(wanted, address.address, address.name):
                return True
        return False
    sender = payload.get(_SENDER_KEY)
    if not isinstance(sender, dict):
        return False
    platform = str(sender.get("platform") or "")
    handle = str(sender.get("handle") or "")
    if normalize_handle(platform, handle) == normalize_handle("*", wanted):
        return True
    return bool(_bare(handle)) and _bare(wanted) == _bare(handle)


def alert_matches(spec: AlertSpec, event: Any) -> bool:
    """Every present condition must hold. Noise never matches: a newsletter
    or automated alert is not something the user asked to be interrupted
    for, even when its words look urgent.

    A turn-based reminder never matches an event; it fires on the clock.
    """
    if spec.is_reminder:
        return False
    if getattr(event, "noise", False):
        return False
    if not _scope_matches(spec.scope, event):
        return False
    if spec.event and spec.event.lower() not in ("", "*") and spec.event != event.kind:
        return False
    if spec.urgency_min and int(getattr(event, "urgency", 0)) < spec.urgency_min:
        return False
    if spec.needs_reply and not getattr(event, "needs_reply", False):
        return False
    if spec.sender and not _sender_matches(spec.sender, event):
        return False
    if spec.contains or spec.description:
        text = enrichment_text(getattr(event, "kind", ""), event.payload or {})
        if spec.contains and spec.contains.lower() not in text.lower():
            return False
        if spec.description and not text_overlap(spec.description, text):
            return False
    return True


@dataclass
class Alert:
    id: int
    spec: AlertSpec
    status: str
    last_event_id: int
    last_run_at: Any
    last_notified_at: Any
    created_at: Any
    converted_from: str = ""

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def channel(self) -> str:
        return self.spec.channel

    def equivalent(self, spec: AlertSpec) -> bool:
        return self.spec.equivalent(spec)


@dataclass
class AlertEvent:
    """The structured fields one notification is rendered from.

    Never the raw payload: a wording pass sees only what a person needs to
    recognise the item, so injected content cannot ride into words.
    """

    alert_id: int
    event_id: int
    source: str
    kind: str
    sender: str = ""
    subject: str = ""
    summary: str = ""
    urgency: int = 0
    occurred_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "event_id": self.event_id,
            "source": self.source,
            "kind": self.kind,
            "sender": self.sender,
            "subject": self.subject,
            "summary": self.summary,
            "urgency": self.urgency,
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_payload(cls, data: Any) -> AlertEvent:
        if not isinstance(data, dict):
            return cls(alert_id=0, event_id=0, source="", kind="")
        return cls(
            alert_id=int(data.get("alert_id") or 0),
            event_id=int(data.get("event_id") or 0),
            source=_clean(data.get("source"), 40),
            kind=_clean(data.get("kind"), 80),
            sender=_clean(data.get("sender"), 200),
            subject=_clean(data.get("subject"), 300),
            summary=_clean(data.get("summary"), 500),
            urgency=int(data.get("urgency") or 0),
            occurred_at=_clean(data.get("occurred_at"), 64),
        )


def event_fields(alert_id: int, event: Any) -> AlertEvent:
    """Pull the recognition fields off one event, deterministically."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    sender = ""
    raw = payload.get("sender")
    if isinstance(raw, dict):
        sender = str(raw.get("handle") or raw.get("name") or "")
    elif raw:
        addresses = parse_addresses(raw)
        if addresses:
            sender = addresses[0].name or addresses[0].address
    subject = str(payload.get("subject") or payload.get("title") or "")
    summary = str(getattr(event, "summary", "") or "")
    occurred = getattr(event, "occurred_at", None)
    return AlertEvent(
        alert_id=alert_id,
        event_id=int(getattr(event, "id", 0) or 0),
        source=str(getattr(event, "source", "") or ""),
        kind=str(getattr(event, "kind", "") or ""),
        sender=_clean(sender, 200),
        subject=_clean(subject, 300),
        summary=_clean(summary, 500),
        urgency=max(0, min(3, int(getattr(event, "urgency", 0) or 0))),
        occurred_at=occurred.isoformat() if hasattr(occurred, "isoformat") else str(occurred or ""),
    )


def dedupe_key(alert_id: int, event_id: int) -> str:
    return f"{alert_id}:{event_id}"
