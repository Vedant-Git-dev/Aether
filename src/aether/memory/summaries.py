"""Thread, daily and weekly summaries — the broad-question layer.

A summary is one sealed row per (kind, period): what happened in a mail
thread, in a day, or in a week. It feeds the brief and stands as a retrieval
candidate for broad questions ("what happened yesterday") that token search
alone answers poorly.

The row keeps a kind word and a keyed blind token over the period in
plaintext; the text and the source event ids are sealed and bound to the row.
The store upserts in place, so refreshing a thread after N new messages is one
statement that never piles up duplicates.

Generation is a model call and rides `memory.summaries`, off by default. The
summarizer is given the already-rendered event lines and returns plain text;
its failure costs the summary, never the turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import asyncpg

from ..llm.types import Message
from .crypto import Cipher

log = logging.getLogger("aether.memory.summaries")

THREAD = "thread"
DAILY = "daily"
WEEKLY = "weekly"
KINDS = (THREAD, DAILY, WEEKLY)

MAX_SUMMARY_CHARS = 1_200


def _aad(summary_id: int) -> str:
    return f"summaries:body_enc:{summary_id}"


@dataclass(frozen=True)
class Summary:
    id: int
    kind: str
    period: str
    text: str
    source_event_ids: tuple[int, ...]
    as_of: datetime
    created_at: datetime


class Summarizer(Protocol):
    async def summarize(self, kind: str, period: str, items: list[str]) -> str:
        """One plain-text summary of the given rendered lines."""


class LLMSummarizer:
    """Adapts any `llm.Provider` into the summarizer: one short turn, plain
    text, no tools."""

    PROMPT = (
        "You write one short summary of the events below for a personal "
        "assistant's memory. Lead with what changed or needs attention; keep "
        "names and absolute dates; write plain prose, no headings, no bullet "
        "lists, no JSON, at most a short paragraph. Never invent anything not "
        "in the events."
    )

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def summarize(self, kind: str, period: str, items: list[str]) -> str:
        if self._provider is None or not items:
            return ""
        text = "\n".join(f"- {item}" for item in items if item)
        turn = await self._provider.complete(
            self.PROMPT, [Message.user(f"({kind} {period})\n{text}")], tools=[]
        )
        return " ".join((turn.text or "").split())[:MAX_SUMMARY_CHARS]


class Summaries:
    """The sealed store of thread, daily and weekly summaries."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    def _token(self, kind: str, period: str) -> bytes:
        return self._cipher.blind_token("summary_period", f"{kind}\x1f{period}")

    async def upsert(
        self,
        kind: str,
        period: str,
        text: str,
        source_event_ids: list[int] | tuple[int, ...] = (),
        *,
        now: datetime | None = None,
    ) -> Summary | None:
        """Write one summary, replacing the row for the same (kind, period).
        An empty text is a bug upstream, not a row."""
        clean = " ".join((text or "").split())[:MAX_SUMMARY_CHARS]
        if kind not in KINDS or not period or not clean:
            return None
        now = now or datetime.now(UTC)
        body = {
            "text": clean,
            "source_event_ids": [int(event_id) for event_id in source_event_ids],
            "as_of": now.isoformat(),
            "period": period,
        }
        token = self._token(kind, period)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO summaries (kind, period_token, body_enc, key_version, created_at)"
                " VALUES ($1, $2, $3, $4, now())"
                " ON CONFLICT (kind, period_token) DO UPDATE SET"
                " body_enc = EXCLUDED.body_enc, key_version = EXCLUDED.key_version,"
                " created_at = now()"
                " RETURNING id, created_at",
                kind,
                token,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._cipher.key_id,
            )
            summary_id = row["id"]
            await conn.execute(
                "UPDATE summaries SET body_enc = $1 WHERE id = $2",
                self._cipher.encrypt_json(body, aad=_aad(summary_id)),
                summary_id,
            )
        return Summary(
            id=summary_id,
            kind=kind,
            period=period,
            text=clean,
            source_event_ids=tuple(body["source_event_ids"]),
            as_of=now,
            created_at=row["created_at"],
        )

    async def get(self, kind: str, period: str) -> Summary | None:
        row = await self._pool.fetchrow(
            "SELECT id, kind, period_token, body_enc, created_at FROM summaries"
            " WHERE kind = $1 AND period_token = $2",
            kind,
            self._token(kind, period),
        )
        return self._to_summary(row) if row else None

    async def recent(self, kind: str, limit: int = 5) -> list[Summary]:
        """The newest summaries of one kind — the read the brief and broad
        retrieval use. Ordering happens in memory: `created_at` is
        bookkeeping, but the period inside the body is not, so Postgres
        cannot sort by it."""
        rows = await self._pool.fetch(
            "SELECT id, kind, period_token, body_enc, created_at FROM summaries"
            " WHERE kind = $1 ORDER BY created_at DESC LIMIT $2",
            kind,
            limit,
        )
        found = [summary for row in rows if (summary := self._to_summary(row)) is not None]
        return found

    def _to_summary(self, row: asyncpg.Record) -> Summary | None:
        summary_id = int(row["id"])
        try:
            body = self._cipher.decrypt_json(row["body_enc"], aad=_aad(summary_id))
        except Exception:
            log.warning("summary %s body undecryptable — skipped", summary_id)
            return None
        if not isinstance(body, dict):
            return None
        return Summary(
            id=summary_id,
            kind=str(row["kind"]),
            period=str(body.get("period") or ""),
            text=str(body.get("text") or ""),
            source_event_ids=tuple(
                int(item) for item in (body.get("source_event_ids") or []) if _is_int(item)
            ),
            as_of=_optional_time(body.get("as_of")) or row["created_at"],
            created_at=row["created_at"],
        )


class SummaryJob:
    """Generate the summaries that are due, one per tick.

    Deterministic scheduling: a thread refreshes after N new messages, each
    past day gets one daily summary, and the previous ISO week gets one
    weekly summary. The model call is the summarizer's; this class only
    decides what is due and reads the events to feed it. One summary per
    tick keeps a backlog from becoming a burst of model calls.
    """

    MIN_LINES = 2
    MAX_LINES = 80

    def __init__(
        self,
        *,
        events: Any,
        summaries: Summaries,
        summarizer: Summarizer | None,
        memory: Any,
        timezone: str = "Asia/Kolkata",
        clock: Any = None,
    ) -> None:
        self._events = events
        self._summaries = summaries
        self._summarizer = summarizer
        self._memory = memory
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    async def tick(self, now: datetime | None = None) -> int:
        """Write at most one due summary. Returns how many were written."""
        if self._summarizer is None or not getattr(self._memory, "summaries", False):
            return 0
        now = now or self._clock()
        try:
            recent = await self._events.recent(limit=400)
        except Exception:
            log.warning("could not read events for a summary", exc_info=True)
            return 0
        if not recent:
            return 0

        for due in (
            await self._thread_candidate(recent),
            await self._daily_candidate(recent, now),
            await self._weekly_candidate(recent, now),
        ):
            if due is None:
                continue
            kind, period, items, source_ids = due
            try:
                text = await self._summarizer.summarize(kind, period, items)
            except Exception:
                log.warning("summarizer failed for %s %s", kind, period, exc_info=True)
                return 0
            if not text:
                return 0
            written = await self._summaries.upsert(kind, period, text, source_ids, now=now)
            return 1 if written is not None else 0
        return 0

    async def _thread_candidate(self, recent: list[Any]):
        threshold = max(1, int(getattr(self._memory, "summary_thread_messages", 10)))
        groups: dict[str, list[Any]] = {}
        for event in recent:
            thread = _thread_of(event)
            if thread:
                groups.setdefault(thread, []).append(event)
        for thread, events in groups.items():
            if len(events) < max(self.MIN_LINES, threshold):
                continue
            existing = await self._summaries.get(THREAD, thread)
            seen = set(existing.source_event_ids) if existing is not None else set()
            fresh = [event for event in events if event.id not in seen]
            if existing is not None and len(fresh) < threshold:
                continue
            items, ids = _render_lines(fresh or events)
            if len(items) >= self.MIN_LINES:
                return THREAD, thread, items[: self.MAX_LINES], ids
        return None

    async def _daily_candidate(self, recent: list[Any], now: datetime):
        groups: dict[str, list[Any]] = {}
        for event in recent:
            groups.setdefault(day_key(event.occurred_at, self._timezone), []).append(event)
        yesterday = day_key(now - timedelta(days=1), self._timezone)
        for day in sorted(groups, reverse=True):
            if day == day_key(now, self._timezone):
                continue  # today is still being written
            if day != yesterday and len(groups[day]) < 3:
                continue
            if await self._summaries.get(DAILY, day) is not None:
                continue
            items, ids = _render_lines(groups[day])
            if len(items) >= self.MIN_LINES:
                return DAILY, day, items[: self.MAX_LINES], ids
        return None

    async def _weekly_candidate(self, recent: list[Any], now: datetime):
        previous = week_key(now - timedelta(days=7), self._timezone)
        if await self._summaries.get(WEEKLY, previous) is not None:
            return None
        events = [
            event for event in recent if week_key(event.occurred_at, self._timezone) == previous
        ]
        items, ids = _render_lines(events)
        if len(items) < self.MIN_LINES:
            return None
        return WEEKLY, previous, items[: self.MAX_LINES], ids


def _thread_of(event: Any) -> str:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("thread_id") or "").strip()


def _render_lines(events: list[Any]) -> tuple[list[str], list[int]]:
    lines: list[str] = []
    ids: list[int] = []
    for event in events:
        if getattr(event, "noise", False):
            continue
        line = _event_line(event)
        if not line:
            continue
        lines.append(line)
        ids.append(int(event.id))
    return lines, ids


def _event_line(event: Any) -> str:
    summary = str(getattr(event, "summary", "") or "").strip()
    if summary:
        return summary
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    for key in ("subject", "snippet", "text", "caption", "filename"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def day_key(when: datetime, timezone: str = "Asia/Kolkata") -> str:
    """The daily summary's period key: the user's own calendar day."""
    from .timewindow import zone

    return when.astimezone(zone(timezone)).date().isoformat()


def week_key(when: datetime, timezone: str = "Asia/Kolkata") -> str:
    """The weekly summary's period key: ISO year-week in the user's zone."""
    from .timewindow import zone

    local = when.astimezone(zone(timezone))
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True
