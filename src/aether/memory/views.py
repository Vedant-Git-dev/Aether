"""Structured views over the resident index (plan §3, §5, C4).

Filtering and sorting cannot run in SQL once the values are sealed, so the
views every "what's urgent?", "what's due?" and "everything with X" question
reads from are built in process memory over `MemoryIndex`:

  - `pending_attention` orders the hot events the way the attention route
    needs them — urgency first, then the soonest deadline, then recency.
  - `commitments_view` is the open set ordered by due time.
  - `entity_timeline` is the person route's "everything with X".

These are substrate: with `memory.enrichment` off nothing ever carries a
urgency or a deadline, so the attention view reads empty rather than
guessing. Phase 2's router is what turns a question into one of these calls;
this module only answers them. `wants_attention` is the deterministic half
of that route — the keyword trigger plan §5.3 names — so a Phase 1 turn can
already surface the view it has.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from .commitments import Commitment, Commitments
from .crypto import Cipher
from .indexes import IndexedEvent, MemoryIndex

# The words plan §5.3 lists as the attention trigger ("kuch urgent hai?",
# "what's pending", "due today"). Cheap and deterministic: the route is a
# lookup, not a model call, and a false positive costs one view read.
_ATTENTION_TERMS = (
    "urgent",
    "asap",
    "pending",
    "deadline",
    "overdue",
    "attention",
    "due today",
    "due tomorrow",
    "needs reply",
    "waiting on",
    "follow up",
    "follow-up",
    "action required",
)
_ATTENTION_RE = re.compile(
    "|".join(rf"\b{re.escape(term)}\b" for term in _ATTENTION_TERMS), re.IGNORECASE
)


def wants_attention(text: str) -> bool:
    """True when the words themselves ask for the attention view."""
    return bool(_ATTENTION_RE.search(text or ""))


@dataclass(frozen=True)
class AttentionItem:
    """One event the attention view kept, with the fields it renders."""

    event_id: int
    source: str
    kind: str
    occurred_at: datetime
    urgency: int
    deadline_at: datetime | None
    summary: str


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class Views:
    """The structured reads over the resident event index."""

    def __init__(
        self,
        index: MemoryIndex,
        commitments: Commitments | None = None,
        *,
        pool: asyncpg.Pool | None = None,
        cipher: Cipher | None = None,
        timezone: str = "Asia/Kolkata",
        cold_batch: int = 500,
    ) -> None:
        self._index = index
        self._commitments = commitments
        self._pool = pool
        self._cipher = cipher
        self._timezone = timezone
        self._cold_batch = cold_batch

    # -- attention ----------------------------------------------------------

    def pending_attention(
        self, *, now: datetime | None = None, limit: int = 20, min_urgency: int = 1
    ) -> list[AttentionItem]:
        """Urgent and due events, ordered urgency desc, deadline asc, recency.

        An undated event sorts behind every dated one at the same urgency —
        "no deadline" is not "due now" — and noise never enters. The set is
        the resident hot set: a question about a very old event cold-scans
        through `entity_timeline`/`ensure_since` instead.
        """
        now = now or datetime.now(UTC)
        items: list[AttentionItem] = []
        for event in self._index.all_events():
            if event.noise or event.urgency < min_urgency:
                continue
            items.append(
                AttentionItem(
                    event_id=event.id,
                    source=event.source,
                    kind=event.kind,
                    occurred_at=event.occurred_at,
                    urgency=event.urgency,
                    deadline_at=event.deadline_at,
                    summary=event.summary,
                )
            )
        items.sort(
            key=lambda item: (
                -item.urgency,
                item.deadline_at is None,
                item.deadline_at or now,
                -item.occurred_at.timestamp(),
            )
        )
        return items[:limit]

    def render_attention(
        self, items: list[AttentionItem], *, now: datetime | None = None, limit: int = 5
    ) -> str:
        """Plain lines for the attention block, deadline rendered in the
        user's timezone and marked overdue once it has passed."""
        now = now or datetime.now(UTC)
        zone = _zone(self._timezone)
        lines: list[str] = []
        for item in items[:limit]:
            parts = [f"urgent({item.urgency})" if item.urgency >= 2 else "noted"]
            if item.deadline_at is not None:
                due = item.deadline_at.astimezone(zone)
                stamp = due.strftime("%Y-%m-%d %H:%M")
                parts.append(f"overdue since {stamp}" if due < now else f"due {stamp}")
            who = f"{item.source}/{item.kind}"
            summary = item.summary or "(no summary)"
            lines.append(f"- {', '.join(parts)}: {who} — {summary}")
        return "\n".join(lines)

    # -- commitments --------------------------------------------------------

    async def commitments_view(self, limit: int = 50) -> list[Commitment]:
        """The open commitments, soonest due first.

        The due time is sealed, so the ordering happens inside the store
        after the bounded open-set read — Postgres is never asked to sort a
        column it cannot read.
        """
        if self._commitments is None:
            return []
        return await self._commitments.list_open(limit=limit)

    def render_commitments(
        self, items: list[Commitment], *, now: datetime | None = None, limit: int = 5
    ) -> str:
        """Plain lines for the open commitments, overdue marked in the
        user's timezone."""
        now = now or datetime.now(UTC)
        zone = _zone(self._timezone)
        lines: list[str] = []
        for commitment in items[:limit]:
            when = ""
            if commitment.due_at is not None:
                due = commitment.due_at.astimezone(zone)
                stamp = due.strftime("%Y-%m-%d %H:%M")
                when = f" — overdue since {stamp}" if due < now else f" — due {stamp}"
            lines.append(f"- {commitment.kind}: {commitment.text}{when}")
        return "\n".join(lines)

    # -- entity timeline ----------------------------------------------------

    async def entity_timeline(
        self, entity_id: int, *, since: datetime | None = None, limit: int = 50
    ) -> list[IndexedEvent]:
        """Every event that references one identity, newest first.

        The `by_entity` map answers from the hot set; a `since` older than
        what the index covers cold-scans in bounded batches until the window
        is complete, never the whole table for one question.
        """
        if since is not None and self._pool is not None and self._cipher is not None:
            with contextlib.suppress(Exception):
                # a cold scan that fails still answers from the hot set
                await self._index.ensure_since(
                    self._pool, self._cipher, since, batch=self._cold_batch
                )
        found = self._index.events_for_entity(entity_id)
        if since is not None:
            found = [event for event in found if event.occurred_at >= since]
        return found[:limit]

    # -- provenance ---------------------------------------------------------

    def attention_payload(
        self, items: list[AttentionItem], *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """The attention view in the trace shape the panel already renders."""
        now = now or datetime.now(UTC)
        return [
            {
                "id": item.event_id,
                "source": item.source,
                "kind": item.kind,
                "urgency": item.urgency,
                "deadline_at": item.deadline_at.isoformat() if item.deadline_at else "",
                "summary": item.summary,
                "overdue": bool(item.deadline_at and item.deadline_at < now),
            }
            for item in items
        ]
