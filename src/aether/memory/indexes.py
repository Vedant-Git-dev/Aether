"""The resident memory index.

Filtering and sorting cannot run in SQL over plaintext content columns, so the
hot set lives here: events by time and sequence, remembered above the salience
threshold, grouped by source. The index is rebuilt at boot from the sealed rows
and updated on every ingest and salience write.

Cold history is scanned in bounded batches and folded into the same index.
`covers(since)` says whether a time window is complete; a query that needs more
calls `ensure_since` rather than reading the whole table.

This module deliberately does not import `events.Event`: the store converts an
`IndexedEvent` into its own dataclass, so the two never create an import cycle.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .crypto import Cipher, CryptoError

log = logging.getLogger("aether.memory.indexes")


@dataclass(frozen=True)
class IndexedEvent:
    """One event as the index holds it — decrypted, in process memory only.

    The enrichment fields are decoded out of `meta` (which is the sealed
    `extra` dict) rather than read from it on every question: an attention
    view sorts the whole hot set, and a dict lookup per event per sort is
    work the index can do once, at decode time. `meta` stays the raw dict,
    so a salience write and an enrichment write compose instead of
    clobbering each other.
    """

    id: int
    source: str
    kind: str
    occurred_at: datetime
    salience_score: float
    memorable: bool
    payload: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    # -- write-time enrichment --
    urgency: int = 0
    needs_reply: bool = False
    noise: bool = False
    deadline_at: datetime | None = None
    summary: str = ""
    entity_ids: tuple[int, ...] = ()

    def with_extra(self, extra: dict[str, Any]) -> IndexedEvent:
        """The same event with its sealed extra replaced — the one place a
        salience or enrichment write re-derives the decoded fields, so the
        two can never disagree about what the extra says."""
        return replace(self, meta=dict(extra), **decode_extra(extra))


def decode_extra(extra: Any) -> dict[str, Any]:
    """The enrichment fields inside one sealed `extra` dict.

    Every value is coerced rather than trusted: this reads back whatever
    an older version of the writer put there, and a malformed field must
    degrade to its default instead of taking a whole turn down.
    """
    data = extra if isinstance(extra, dict) else {}
    deadline = data.get("deadline_at")
    parsed = _optional_time(deadline)
    entities: list[int] = []
    for item in data.get("entities") or []:
        try:
            entities.append(int(item))
        except (TypeError, ValueError):
            continue
    try:
        urgency = int(data.get("urgency") or 0)
    except (TypeError, ValueError):
        urgency = 0
    return {
        "urgency": max(0, min(3, urgency)),
        "needs_reply": bool(data.get("needs_reply", False)),
        "noise": bool(data.get("noise", False)),
        "deadline_at": parsed,
        "summary": str(data.get("summary") or ""),
        "entity_ids": tuple(dict.fromkeys(entities)),
    }


def decode_event_row(cipher: Cipher, row: asyncpg.Record) -> IndexedEvent:
    """One events row, sealed only. Raises CryptoError on unreadable content
    so callers can skip and count it."""
    event_id = int(row["id"])
    payload = cipher.decrypt_json(row["payload_enc"], aad=f"events:payload_enc:{event_id}")
    if not row.get("meta_enc"):
        raise CryptoError(f"event {event_id} has no sealed metadata")
    meta = cipher.decrypt_json(row["meta_enc"], aad=f"events:meta_enc:{event_id}")
    extra = dict(meta.get("extra") or {})
    return IndexedEvent(
        id=event_id,
        source=str(meta.get("source", "")),
        kind=str(meta.get("kind", "")),
        occurred_at=_parse_time(meta.get("occurred_at"), row.get("created_at")),
        salience_score=float(meta.get("salience", 0.0)),
        memorable=bool(meta.get("memorable", False)),
        payload=payload,
        meta=extra,
        **decode_extra(extra),
    )


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


_EVENT_COLUMNS = "id, payload_enc, meta_enc, created_at"


class MemoryIndex:
    """The hot event table, decrypted and ordered in process memory."""

    def __init__(self) -> None:
        self._events: dict[int, IndexedEvent] = {}
        self._order: list[tuple[datetime, int]] = []
        # entity id -> the events that reference it, for the person route's
        # timeline. Maintained alongside the event table rather than derived
        # on demand: the question "everything with X" is a lookup, not a
        # scan of the whole hot set.
        self._by_entity: dict[int, set[int]] = {}
        self._max_id = 0
        # oldest occurrence time the index is known to cover; None = nothing
        self._covers_from: datetime | None = None
        # next cold-scan bound: fetch rows with id < this, whether or not the
        # previous batch yielded anything indexable. Keeps an all-unreadable
        # batch from being re-queried forever.
        self._scan_from: int | None = None
        self.skipped = 0

    # -- lookups ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    @property
    def max_id(self) -> int:
        return self._max_id

    def get(self, event_id: int) -> IndexedEvent | None:
        return self._events.get(event_id)

    def covers(self, since: datetime) -> bool:
        return self._covers_from is not None and self._covers_from <= since

    def recent(self, limit: int = 50) -> list[IndexedEvent]:
        return [self._events[event_id] for _, event_id in reversed(self._order[-limit:])]

    def recent_memorable(self, limit: int = 50) -> list[IndexedEvent]:
        out: list[IndexedEvent] = []
        for _, event_id in reversed(self._order):
            event = self._events[event_id]
            if event.memorable:
                out.append(event)
                if len(out) >= limit:
                    break
        return out

    def all_events(self) -> list[IndexedEvent]:
        """Every resident event, unordered — the attention view sorts the
        hot set itself, and the index does not keep an urgency order."""
        return list(self._events.values())

    def since_time(self, since: datetime, limit: int = 2_000) -> list[IndexedEvent]:
        """Oldest first among events at or after `since`, capped."""
        start = bisect.bisect_left(self._order, (since, -1))
        return [self._events[event_id] for _, event_id in self._order[start : start + limit]]

    def after_id(self, after_id: int, limit: int = 50) -> list[IndexedEvent]:
        found = [e for e in self._events.values() if e.id > after_id]
        found.sort(key=lambda e: e.id)
        return found[:limit]

    def count_since(self, source: str, since: datetime) -> int:
        return sum(
            1
            for event in self._events.values()
            if event.source == source and event.occurred_at >= since
        )

    def count_memorable_since(self, since: datetime) -> int:
        return sum(
            1 for event in self._events.values() if event.memorable and event.occurred_at >= since
        )

    # -- maintenance --------------------------------------------------------

    def upsert(self, event: IndexedEvent) -> None:
        previous = self._events.get(event.id)
        if previous is not None:
            old = (previous.occurred_at, previous.id)
            at = bisect.bisect_left(self._order, old)
            if at < len(self._order) and self._order[at] == old:
                self._order.pop(at)
            self._unindex_entities(previous)
        bisect.insort(self._order, (event.occurred_at, event.id))
        self._events[event.id] = event
        self._index_entities(event)
        self._max_id = max(self._max_id, event.id)
        if self._covers_from is None or event.occurred_at < self._covers_from:
            self._covers_from = event.occurred_at

    def update_salience(
        self, event_id: int, score: float, memorable: bool, extra: dict[str, Any]
    ) -> None:
        event = self._events.get(event_id)
        if event is None:
            return
        self._unindex_entities(event)
        updated = replace(event.with_extra(extra), salience_score=score, memorable=memorable)
        self._events[event_id] = updated
        self._index_entities(updated)

    def update_extra(self, event_id: int, extra: dict[str, Any]) -> None:
        """Fold an enrichment write into a resident event — the same path a
        salience write takes, so the decoded fields follow the sealed dict."""
        event = self._events.get(event_id)
        if event is None:
            return
        self._unindex_entities(event)
        updated = event.with_extra(extra)
        self._events[event_id] = updated
        self._index_entities(updated)

    # -- entity lookups ------------------------------------------------------

    def events_for_entity(self, entity_id: int) -> list[IndexedEvent]:
        """Every resident event that references an identity, newest first."""
        found = [self._events[i] for i in self._by_entity.get(entity_id, ()) if i in self._events]
        found.sort(key=lambda event: (event.occurred_at, event.id), reverse=True)
        return found

    def _index_entities(self, event: IndexedEvent) -> None:
        for entity_id in event.entity_ids:
            self._by_entity.setdefault(entity_id, set()).add(event.id)

    def _unindex_entities(self, event: IndexedEvent) -> None:
        for entity_id in event.entity_ids:
            ids = self._by_entity.get(entity_id)
            if ids is None:
                continue
            ids.discard(event.id)
            if not ids:
                del self._by_entity[entity_id]

    def _trim(self, hot_limit: int) -> None:
        """Keep the newest `hot_limit` events resident; older ones leave the
        index and are cold-scanned again if a query asks for them."""
        if hot_limit <= 0 or len(self._events) <= hot_limit:
            return
        keep = {event_id for _, event_id in self._order[-hot_limit:]}
        for event_id in [i for i in self._events if i not in keep]:
            self._unindex_entities(self._events[event_id])
            del self._events[event_id]
        self._order = [(when, event_id) for when, event_id in self._order if event_id in keep]
        self._covers_from = self._order[0][0] if self._order else None

    # -- building from the database ----------------------------------------

    async def rebuild(self, pool: asyncpg.Pool, cipher: Cipher, *, hot_limit: int = 2_000) -> int:
        """Load the newest events by sequence — the boot pass. Returns how
        many rows were read (undecryptable ones are skipped and counted)."""
        rows = await pool.fetch(
            f"SELECT {_EVENT_COLUMNS} FROM events ORDER BY id DESC LIMIT $1", hot_limit
        )
        self._events.clear()
        self._order.clear()
        self._by_entity.clear()
        self._max_id = 0
        self._covers_from = None
        self._scan_from = None
        self.skipped = 0
        for row in reversed(rows):
            try:
                self.upsert(decode_event_row(cipher, row))
            except Exception:
                self.skipped += 1
                log.warning("event %s undecryptable — left out of the index", row["id"])
        self._trim(hot_limit)
        log.info(
            "memory index rebuilt: %d event(s), covered from %s, %d skipped",
            len(self._events),
            self._covers_from.isoformat() if self._covers_from else "nothing",
            self.skipped,
        )
        return len(self._events)

    async def ensure_since(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher,
        since: datetime,
        *,
        batch: int = 500,
        max_batches: int = 20,
    ) -> None:
        """Cold-scan older events until `since` is covered, in bounded batches."""
        for _ in range(max_batches):
            if self.covers(since):
                return
            oldest_id = self._scan_from
            if oldest_id is None:
                oldest_id = min(self._events) if self._events else 0
            if oldest_id <= 1:
                self._covers_from = datetime.min.replace(tzinfo=UTC)
                return
            rows = await pool.fetch(
                f"SELECT {_EVENT_COLUMNS} FROM events WHERE id < $1 ORDER BY id DESC LIMIT $2",
                oldest_id,
                batch,
            )
            if not rows:
                self._covers_from = datetime.min.replace(tzinfo=UTC)
                return
            oldest: datetime | None = None
            for row in reversed(rows):
                try:
                    event = decode_event_row(cipher, row)
                except Exception:
                    self.skipped += 1
                    log.warning("event %s undecryptable — left out of the index", row["id"])
                    continue
                self.upsert(event)
                if oldest is None or event.occurred_at < oldest:
                    oldest = event.occurred_at
            # advance past this batch even when nothing in it decoded, so a
            # run of unreadable rows cannot stall the scan on the same window
            self._scan_from = int(rows[-1]["id"])
            if oldest is None:
                # nothing decoded: bound coverage by the plaintext created_at
                # of the oldest row rather than trusting an unreadable time
                created = rows[-1].get("created_at")
                oldest = created if isinstance(created, datetime) else None
            if oldest is not None:
                self._covers_from = (
                    oldest if self._covers_from is None else min(self._covers_from, oldest)
                )
