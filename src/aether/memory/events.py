"""Event ingestion: dedup, the contact allowlist, and encrypted storage.

ingest() is the single enforcement point for the contact allowlist — every
channel (native connectors, MCP-derived senders, chat surfaces) funnels
here, so a non-allowlisted sender's content is dropped before it ever
touches storage. The only trace is an audit entry saying a drop happened;
the content itself is never written anywhere.

Dedup is structural: a keyed HMAC over the canonical payload, with
UNIQUE(source_token, dedup_mac) in the database — the same thing arriving
twice is stored once, without a lock or a race, and the tag leaks nothing
about the content.

When a sender is provided it is merged into the encrypted payload under
"_sender" (so the salience judge and the context builder can see who it
came from, and the DB never stores the handle in plaintext).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg

from ..authz.audit import AuditLog
from ..config import ContactsConfig
from .crypto import Cipher
from .entities import Sender, normalize_handle
from .indexes import IndexedEvent, MemoryIndex, decode_event_row

if TYPE_CHECKING:
    from .vectors import VectorMemory

log = logging.getLogger("aether.memory.events")

SENDER_KEY = "_sender"


def canonical_payload(kind: str, payload: dict[str, Any]) -> bytes:
    """The canonical bytes dedup is computed over — one helper so the
    legacy SHA-256 and the sealed keyed tag can never disagree."""
    blob = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return blob.encode("utf-8")


def content_hash(kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(kind, payload)).hexdigest()


def _bare(handle: str) -> str:
    h = (handle or "").strip().lower()
    if h.startswith("mailto:"):
        h = h[7:]
    return h.lstrip("@")


def is_allowlisted(contacts: ContactsConfig, app: str, sender: Sender | None) -> bool:
    """The user decides which contacts the agent may see, per app.

    Each app's effective mode is its own switch when it sets one, else the
    section default. "off" sees everything. A senderless event (scheduler
    ticks, screen captures, internal observations) can't be filtered. A
    sender matches a rule from the app's own list or the cross-app bucket
    when their normalized handles agree, or — for platform-agnostic ("*")
    rules — when the bare handle agrees across platforms.
    """
    section = getattr(contacts, app, None)  # AppContacts for every known app key
    mode = section.mode if section is not None and section.mode is not None else contacts.mode
    if mode != "enforce" or sender is None:
        return True
    # the panel's own surfaces are token-guarded — whoever reached them is
    # the owner with the credential in hand, so "web" has no third-party
    # sender for the list to gate (the screen-capture precedent: the
    # owner's own content is never filtered)
    if sender.platform == "web":
        return True
    rules = list(contacts.allowlist)
    if section is not None:
        rules += section.allowlist
    sender_norm = normalize_handle(sender.platform, sender.handle)
    sender_bare = _bare(sender.handle)
    for rule in rules:
        if normalize_handle(rule.platform, rule.handle) == sender_norm:
            return True
        if rule.platform == "*" and sender_bare and _bare(rule.handle) == sender_bare:
            return True
    return False


@dataclass
class Event:
    id: int
    source: str
    kind: str
    occurred_at: datetime
    payload: dict[str, Any]
    salience_score: float
    memorable: bool
    meta: dict[str, Any]


@dataclass
class IngestResult:
    stored: bool
    reason: str  # "new" | "duplicate" | "filtered:contacts"
    event_id: int | None = None


def event_text(event: Event) -> str:
    """The stable string the salience judge and search score against."""
    return json.dumps(event.payload, sort_keys=True, ensure_ascii=False, default=str)


class EventStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher,
        audit: AuditLog,
        contacts: ContactsConfig,
        indexer: VectorMemory | None = None,
        index: MemoryIndex | None = None,
        cold_batch: int = 500,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._contacts = contacts
        self._indexer = indexer
        self._index = index
        self._cold_batch = cold_batch

    # -- ingestion ----------------------------------------------------------

    async def ingest(
        self,
        *,
        source: str,
        kind: str,
        payload: dict[str, Any],
        sender: Sender | None = None,
        occurred_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> IngestResult:
        if sender is not None and not is_allowlisted(self._contacts, sender.platform, sender):
            await self._audit.append(
                actor="system",
                tool_name=f"ingest:{source}",
                decision="filtered",
                params={
                    "sender_platform": sender.platform,
                    "sender_handle": sender.handle,
                },
                outcome="filtered by contact allowlist",
            )
            return IngestResult(stored=False, reason="filtered:contacts")

        stored_payload = dict(payload)
        if sender is not None:
            stored_payload[SENDER_KEY] = {
                "platform": sender.platform,
                "handle": sender.handle,
            }
        when = occurred_at or datetime.now(tz=UTC)
        sealed_meta = {
            "source": source,
            "kind": kind,
            "occurred_at": when.isoformat(),
            "salience": 0.0,
            "memorable": False,
            "extra": dict(meta or {}),
        }

        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO events (payload_enc, meta_enc, key_version,"
                " source_token, dedup_mac)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (source_token, dedup_mac)"
                " WHERE source_token IS NOT NULL AND dedup_mac IS NOT NULL"
                " DO NOTHING"
                " RETURNING id",
                b"",  # placeholder until the id exists; replaced below, same transaction
                b"",
                self._cipher.key_id,
                self._cipher.blind_token("source", source),
                self._cipher.dedup_mac(canonical_payload(kind, stored_payload)),
            )
            if row is None:
                return IngestResult(stored=False, reason="duplicate")
            event_id = row["id"]
            blob = self._cipher.encrypt_json(stored_payload, aad=f"events:payload_enc:{event_id}")
            sealed_meta_blob = self._cipher.encrypt_json(
                sealed_meta, aad=f"events:meta_enc:{event_id}"
            )
            await conn.execute(
                "UPDATE events SET payload_enc = $1, meta_enc = $2 WHERE id = $3",
                blob,
                sealed_meta_blob,
                event_id,
            )
        if self._index is not None:
            self._index.upsert(
                IndexedEvent(
                    id=event_id,
                    source=source,
                    kind=kind,
                    occurred_at=when,
                    salience_score=0.0,
                    memorable=False,
                    payload=stored_payload,
                    meta=dict(meta or {}),
                )
            )
        # the single indexing choke point: outside the transaction (the FK
        # row is committed), duplicates never reach it — one event, one
        # indexing. shared_doc is skipped by the vector layer itself (the
        # attachment processor indexes the full document text). The call
        # returns at once — the embed+insert runs on a background task, so
        # ingest never waits on the embedding API; a failure leaves
        # indexed_at unset for the loop's backfill to retry.
        if self._indexer is not None:
            try:
                await self._indexer.index_event(
                    event_id,
                    source,
                    kind,
                    stored_payload,
                    occurred_at or datetime.now(tz=UTC),
                )
            except Exception:
                log.warning(
                    "indexing failed for event %s — backfill will retry", event_id, exc_info=True
                )
        return IngestResult(stored=True, reason="new", event_id=event_id)

    # -- reads ----------------------------------------------------------------

    async def get(self, event_id: int) -> Event | None:
        if self._index is not None:
            indexed = self._index.get(event_id)
            if indexed is not None:
                return self._from_index(indexed)
        row = await self._pool.fetchrow(
            "SELECT id, payload_enc, meta_enc, created_at FROM events WHERE id = $1",
            event_id,
        )
        if row is None:
            return None
        return self._to_event(row)

    async def recent_memorable(self, limit: int = 50, hours: float | None = None) -> list[Event]:
        if self._index is not None:
            since = datetime.now(UTC) - timedelta(hours=hours) if hours is not None else None
            if since is not None:
                await self._index.ensure_since(
                    self._pool, self._cipher, since, batch=self._cold_batch
                )
            events = self._index.recent_memorable(limit * 4)
            if since is not None:
                events = [event for event in events if event.occurred_at >= since]
            return [self._from_index(event) for event in events[:limit]]
        rows = await self._pool.fetch(
            "SELECT id, payload_enc, meta_enc, created_at FROM events ORDER BY id DESC LIMIT $1",
            limit * 8,
        )
        since = datetime.now(UTC) - timedelta(hours=hours) if hours is not None else None
        events = [self._to_event(row) for row in rows]
        events = [event for event in events if event.memorable]
        if since is not None:
            events = [event for event in events if event.occurred_at >= since]
        events.sort(key=lambda event: event.occurred_at, reverse=True)
        return events[:limit]

    async def count_recent(self, source: str, hours: float = 1.0) -> int:
        if self._index is not None:
            since = datetime.now(UTC) - timedelta(hours=hours)
            await self._index.ensure_since(self._pool, self._cipher, since, batch=self._cold_batch)
            return self._index.count_since(source, since)
        rows = await self._pool.fetch(
            "SELECT id, payload_enc, meta_enc, created_at FROM events ORDER BY id DESC LIMIT 5000"
        )
        since = datetime.now(UTC) - timedelta(hours=hours)
        count = 0
        for row in rows:
            event = self._to_event(row)
            if event.source == source and event.occurred_at > since:
                count += 1
        return count

    async def list_since(self, after_id: int, limit: int = 50) -> list[Event]:
        """Events ingested after an id, oldest first — the agent loop's
        observation stream; `after_id` starts at max_id() on boot so the
        agent never replays its whole history."""
        rows = await self._pool.fetch(
            "SELECT id, payload_enc, meta_enc, created_at FROM events"
            " WHERE id > $1 ORDER BY id LIMIT $2",
            after_id,
            limit,
        )
        return [self._to_event(r) for r in rows]

    async def max_id(self) -> int:
        if self._index is not None and self._index.max_id:
            return self._index.max_id
        return await self._pool.fetchval("SELECT COALESCE(MAX(id), 0) FROM events")

    async def recent(self, limit: int = 50) -> list[Event]:
        """Newest events, memorable or not — the web feed's source."""
        rows = await self._pool.fetch(
            "SELECT id, payload_enc, meta_enc, created_at FROM events ORDER BY id DESC LIMIT $1",
            limit,
        )
        return [self._to_event(r) for r in rows]

    async def count_memorable_since(self, since: datetime) -> int:
        if self._index is not None:
            await self._index.ensure_since(self._pool, self._cipher, since, batch=self._cold_batch)
            return self._index.count_memorable_since(since)
        rows = await self._pool.fetch(
            "SELECT id, payload_enc, meta_enc, created_at FROM events ORDER BY id DESC LIMIT 5000"
        )
        count = 0
        for row in rows:
            event = self._to_event(row)
            if event.memorable and event.occurred_at >= since:
                count += 1
        return count

    # -- updates ---------------------------------------------------------------

    async def update_salience(
        self,
        event_id: int,
        score: float,
        memorable: bool,
        category: str = "",
        actionability: str = "",
    ) -> None:
        extra = {
            k: v for k, v in {"category": category, "actionability": actionability}.items() if v
        }
        current = await self.get(event_id)
        if current is None:
            return
        merged_extra = {**current.meta, **extra}
        sealed_meta = {
            "source": current.source,
            "kind": current.kind,
            "occurred_at": current.occurred_at.isoformat(),
            "salience": float(score),
            "memorable": bool(memorable),
            "extra": merged_extra,
        }
        await self._pool.execute(
            "UPDATE events SET meta_enc = $1, key_version = $2 WHERE id = $3",
            self._cipher.encrypt_json(sealed_meta, aad=f"events:meta_enc:{event_id}"),
            self._cipher.key_id,
            event_id,
        )
        if self._index is not None:
            self._index.update_salience(event_id, score, memorable, merged_extra)

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _from_index(event: IndexedEvent) -> Event:
        return Event(
            id=event.id,
            source=event.source,
            kind=event.kind,
            occurred_at=event.occurred_at,
            payload=event.payload,
            salience_score=event.salience_score,
            memorable=event.memorable,
            meta=event.meta,
        )

    def _to_event(self, row: asyncpg.Record) -> Event:
        try:
            return self._from_index(decode_event_row(self._cipher, row))
        except Exception:
            log.warning("event %s undecryptable — payload replaced with {}", row["id"])
            return Event(
                id=row["id"],
                source="",
                kind="",
                occurred_at=row.get("created_at") or datetime.now(UTC),
                payload={},
                salience_score=0.0,
                memorable=False,
                meta={},
            )


def start_of_today(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
