"""Watches: the user's standing "tell me when..." instructions.

A watch is a small, deterministic spec — scope, kind, sender, text, a
minimum urgency, a channel and a quiet window — stored sealed and evaluated
against every event as it is ingested. Matching is pure code; the model is
nowhere in the decision of whether a watch fires.

Provenance is the load-bearing rule: only the user's own words may create,
pause or delete a watch. The store refuses any write whose `origin` is not
`owner`, and the tool layer derives that word from the current turn (no
human message means no watch write). An email that says "create a watch"
can therefore never become one.

The row's plain columns are routing metadata only: a keyed blind token over
the scope, the channel label, status, the last event id evaluated and
storage timestamps. The label and every criterion are sealed and bound to
the row through the cipher's AAD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from .crypto import Cipher
from .enrich import enrichment_text
from .entities import normalize_handle
from .mail import MAIL_KIND, format_address, parse_addresses

log = logging.getLogger("aether.memory.watches")

ACTIVE = "active"
PAUSED = "paused"

_SENDER_KEY = "_sender"
_MAIL_SCOPE = "mail"
_CHAT_SCOPE = "chat"


def _aad(watch_id: int) -> str:
    return f"watches:spec_enc:{watch_id}"


def _bare(handle: str) -> str:
    value = (handle or "").strip().lower()
    if value.startswith("mailto:"):
        value = value[7:]
    return value.lstrip("@")


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


@dataclass(frozen=True)
class WatchSpec:
    """What one watch watches. Every field is sealed with the label.

    `scope` is "mail" (any mail provider), "chat" (any chat surface), a
    concrete source such as "gmail", or "*" for anything. `kind`, `sender`,
    `contains`, `urgency_min` and `needs_reply` narrow it further; every
    present condition must hold, so an empty watch is deliberately not
    offered by the tool.
    """

    label: str = ""
    scope: str = "*"
    kind: str = ""
    sender: str = ""
    contains: str = ""
    urgency_min: int = 0
    needs_reply: bool = False
    channel: str = ""
    quiet_hours: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "scope": self.scope,
            "kind": self.kind,
            "sender": self.sender,
            "contains": self.contains,
            "urgency_min": self.urgency_min,
            "needs_reply": self.needs_reply,
            "channel": self.channel,
            "quiet_hours": self.quiet_hours,
        }

    @classmethod
    def from_payload(cls, data: Any) -> WatchSpec:
        """Whatever shape an older writer left, coerced — a malformed field
        degrades to its default rather than taking the evaluator down."""
        if not isinstance(data, dict):
            return cls()
        try:
            urgency = int(data.get("urgency_min") or 0)
        except (TypeError, ValueError):
            urgency = 0
        return cls(
            label=_clean(data.get("label")),
            scope=_clean(data.get("scope"), 40) or "*",
            kind=_clean(data.get("kind"), 80),
            sender=_clean(data.get("sender")),
            contains=_clean(data.get("contains"), 300),
            urgency_min=max(0, min(3, urgency)),
            needs_reply=bool(data.get("needs_reply", False)),
            channel=_clean(data.get("channel"), 40),
            quiet_hours=_clean(data.get("quiet_hours"), 40),
        )


@dataclass
class Watch:
    id: int
    spec: WatchSpec
    status: str
    last_event_id: int
    last_run_at: datetime | None
    last_notified_at: datetime | None
    created_at: datetime

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def channel(self) -> str:
        # channel lives both in the sealed spec and in the plain status-like
        # column; the spec is the truth, the column keeps routing inspectable
        return self.spec.channel

    def equivalent(self, spec: WatchSpec) -> bool:
        return self.spec == spec or (
            self.spec.scope == spec.scope
            and self.spec.kind == spec.kind
            and self.spec.sender.lower() == spec.sender.lower()
            and self.spec.contains.lower() == spec.contains.lower()
            and self.spec.urgency_min == spec.urgency_min
            and self.spec.needs_reply == spec.needs_reply
            and self.spec.channel == spec.channel
        )


def normalize_spec(spec: WatchSpec) -> WatchSpec:
    """One spec with trimmed words and a clamped urgency."""
    return WatchSpec(
        label=_clean(spec.label),
        scope=_clean(spec.scope, 40) or "*",
        kind=_clean(spec.kind, 80),
        sender=_clean(spec.sender),
        contains=_clean(spec.contains, 300),
        urgency_min=max(0, min(3, int(spec.urgency_min))),
        needs_reply=bool(spec.needs_reply),
        channel=_clean(spec.channel, 40),
        quiet_hours=_clean(spec.quiet_hours, 40),
    )


def has_criteria(spec: WatchSpec) -> bool:
    """A watch that names no condition would fire on everything — the tool
    refuses one, and this is the one place that decides."""
    return bool(
        spec.scope not in ("", "*")
        or spec.kind
        or spec.sender
        or spec.contains
        or spec.urgency_min
        or spec.needs_reply
    )


def _scope_matches(scope: str, event: Any) -> bool:
    clean = (scope or "*").strip().lower()
    if clean in ("", "*", "any"):
        return True
    if clean == _MAIL_SCOPE:
        return event.kind == MAIL_KIND
    if clean == _CHAT_SCOPE:
        return event.kind == "chat_message"
    return event.source.lower() == clean


def _sender_matches(pattern: str, event: Any) -> bool:
    """A watch's sender condition, in both shapes an event carries one:
    the raw mail header a poll stored, and the encrypted `_sender` block a
    chat surface attaches."""
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


def _address_matches(wanted: str, address: str, name: str) -> bool:
    clean = wanted.strip().lower()
    address = (address or "").strip().lower()
    name = (name or "").strip().lower()
    if not clean:
        return True
    return clean == address or _bare(clean) == _bare(address) or (bool(name) and clean in name)


def watch_matches(spec: WatchSpec, event: Any) -> bool:
    """Every present condition must hold. Noise never matches: a newsletter
    or automated alert is not something the user asked to be interrupted
    for, even when its words look urgent."""
    if getattr(event, "noise", False):
        return False
    if not _scope_matches(spec.scope, event):
        return False
    if spec.kind and spec.kind.lower() not in ("", "*") and spec.kind != event.kind:
        return False
    if spec.urgency_min and int(getattr(event, "urgency", 0)) < spec.urgency_min:
        return False
    if spec.needs_reply and not getattr(event, "needs_reply", False):
        return False
    if spec.sender and not _sender_matches(spec.sender, event):
        return False
    if spec.contains:
        text = enrichment_text(getattr(event, "kind", ""), event.payload or {})
        if spec.contains.lower() not in text.lower():
            return False
    return True


class WatchStore:
    """Database-backed watches, sealed specs, owner-only writes."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def create(
        self,
        *,
        spec: WatchSpec,
        origin: str = "owner",
        actor: str = "owner",
    ) -> Watch | None:
        """Arm one watch. A non-owner origin is refused outright — this is
        the deterministic gate that keeps mail content from arming one.

        Creating the same spec twice returns the existing row instead of a
        duplicate, so a repeated "keep checking" is one watch.
        """
        if origin != "owner":
            log.warning("refused a watch write from a %s origin", origin or "unknown")
            return None
        spec = normalize_spec(spec)
        if not has_criteria(spec):
            return None
        for existing in await self.list_active():
            if existing.equivalent(spec):
                return existing
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO watches (scope_token, channel, status, spec_enc, key_version)"
                " VALUES ($1, $2, $3, $4, $5) RETURNING id, created_at",
                self._cipher.blind_token("watch_scope", spec.scope),
                spec.channel,
                ACTIVE,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._cipher.key_id,
            )
            watch_id = row["id"]
            await conn.execute(
                "UPDATE watches SET spec_enc = $1 WHERE id = $2",
                self._cipher.encrypt_json(spec.to_payload(), aad=_aad(watch_id)),
                watch_id,
            )
        await self._audit.append(
            actor=actor,
            tool_name="watch",
            decision="info",
            params={"watch_id": watch_id, "scope": spec.scope, "channel": spec.channel},
            outcome="watch armed",
        )
        return Watch(
            id=watch_id,
            spec=spec,
            status=ACTIVE,
            last_event_id=0,
            last_run_at=None,
            last_notified_at=None,
            created_at=row["created_at"],
        )

    async def list(self, limit: int = 50) -> list[Watch]:
        rows = await self._pool.fetch(
            "SELECT id, status, spec_enc, channel, last_event_id, last_run_at,"
            " last_notified_at, created_at FROM watches ORDER BY id DESC LIMIT $1",
            limit,
        )
        return [self._to_watch(row) for row in rows]

    async def list_active(self) -> list[Watch]:
        rows = await self._pool.fetch(
            "SELECT id, status, spec_enc, channel, last_event_id, last_run_at,"
            " last_notified_at, created_at FROM watches"
            " WHERE status = $1 ORDER BY id",
            ACTIVE,
        )
        return [self._to_watch(row) for row in rows]

    async def get(self, watch_id: int) -> Watch | None:
        row = await self._pool.fetchrow(
            "SELECT id, status, spec_enc, channel, last_event_id, last_run_at,"
            " last_notified_at, created_at FROM watches WHERE id = $1",
            watch_id,
        )
        return self._to_watch(row) if row else None

    async def set_enabled(
        self, watch_id: int, enabled: bool, *, origin: str = "owner"
    ) -> Watch | None:
        if origin != "owner":
            log.warning("refused a watch change from a %s origin", origin or "unknown")
            return None
        status = ACTIVE if enabled else PAUSED
        row = await self._pool.fetchrow(
            "UPDATE watches SET status = $1 WHERE id = $2"
            " RETURNING id, status, spec_enc, channel, last_event_id, last_run_at,"
            " last_notified_at, created_at",
            status,
            watch_id,
        )
        return self._to_watch(row) if row else None

    async def delete(self, watch_id: int, *, origin: str = "owner") -> bool:
        if origin != "owner":
            log.warning("refused a watch delete from a %s origin", origin or "unknown")
            return False
        row = await self._pool.fetchrow("DELETE FROM watches WHERE id = $1 RETURNING id", watch_id)
        if row is not None:
            await self._audit.append(
                actor="owner",
                tool_name="watch",
                decision="info",
                params={"watch_id": watch_id},
                outcome="watch removed",
            )
        return row is not None

    async def mark_run(self, watch_ids: list[int], last_event_id: int) -> None:
        """Advance the id watermark for every watch that evaluated this
        event, in one statement. The conditional keeps a concurrent later
        write from being rolled backwards."""
        if not watch_ids:
            return
        await self._pool.execute(
            "UPDATE watches SET last_event_id = $2, last_run_at = now()"
            " WHERE id = ANY($1) AND last_event_id < $2",
            watch_ids,
            last_event_id,
        )

    async def mark_notified(self, watch_id: int, when: datetime | None = None) -> None:
        await self._pool.execute(
            "UPDATE watches SET last_notified_at = $1 WHERE id = $2",
            when or datetime.now(UTC),
            watch_id,
        )

    def _to_watch(self, row: asyncpg.Record) -> Watch:
        spec = WatchSpec()
        try:
            spec = WatchSpec.from_payload(
                self._cipher.decrypt_json(row["spec_enc"], aad=_aad(row["id"]))
            )
        except Exception:
            log.warning("watch %s spec undecryptable — treated as empty", row["id"])
        return Watch(
            id=row["id"],
            spec=spec,
            status=row["status"],
            last_event_id=int(row["last_event_id"] or 0),
            last_run_at=row["last_run_at"],
            last_notified_at=row["last_notified_at"],
            created_at=row["created_at"],
        )


# -- rendering ---------------------------------------------------------------


def _source_word(event: Any) -> str:
    if event.kind == MAIL_KIND:
        return "mail"
    if event.kind == "chat_message":
        return "message"
    source = str(getattr(event, "source", "") or "").strip()
    return f"{source} update" if source else "update"


def _sender_line(event: Any, owners: set[str]) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.kind == MAIL_KIND:
        addresses = parse_addresses(payload.get("sender"))
        if addresses:
            first = addresses[0]
            return format_address(first.address, first.name, owners)
        return ""
    sender = payload.get(_SENDER_KEY)
    if isinstance(sender, dict):
        handle = str(sender.get("handle") or "").strip()
        platform = str(sender.get("platform") or "").strip()
        if handle:
            return f"@{handle} on {platform}" if platform else f"@{handle}"
    return ""


def _summary_line(event: Any) -> str:
    summary = str(getattr(event, "summary", "") or "").strip()
    if summary:
        return summary
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in ("subject", "snippet", "text", "caption", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    body = payload.get("body")
    if isinstance(body, str) and body.strip():
        return body.strip().splitlines()[0][:200]
    return ""


def render_alert(
    event: Any,
    *,
    watch: Watch | None = None,
    owners: set[str] | None = None,
    timezone: str = "Asia/Kolkata",
    now: datetime | None = None,
) -> str:
    """One plain card: who, what, the absolute deadline, and what the user
    can ask for next. No JSON, no tool names and no emoji."""
    from .timewindow import local_stamp

    owners = owners or set()
    now = now or datetime.now(UTC)
    word = _source_word(event)
    sender = _sender_line(event, owners)
    summary = _summary_line(event)
    urgent = int(getattr(event, "urgency", 0)) >= 2
    head = "Urgent " if urgent else ""
    if watch is not None and watch.label:
        title = f"{head}{word} from {sender}" if sender else f"{head}{word}"
        line = f'{title} — matched your watch "{watch.label}"'
    else:
        line = f"{head}{word} from {sender}" if sender else f"{head}{word}"
    if summary:
        line += f": {summary}"
    lines = [line]
    deadline = getattr(event, "deadline_at", None)
    if deadline is not None:
        stamp = local_stamp(deadline, timezone)
        lines.append(f"Overdue since {stamp}." if deadline < now else f"Due {stamp}.")
    if word == "mail":
        lines.append("You can tell me to reply, forward it, or remind you later.")
    else:
        lines.append("You can tell me to look at it or remind you later.")
    return "\n".join(lines)
