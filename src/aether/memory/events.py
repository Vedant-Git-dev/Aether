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

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg

from ..authz.audit import AuditLog
from ..config import ContactsConfig
from .commitments import Commitments
from .crypto import Cipher
from .enrich import Enricher, ExtractedFact, enrichment_text, heuristic_floor
from .entities import Entities, Sender, normalize_handle
from .facts import Facts
from .indexes import IndexedEvent, MemoryIndex, decode_event_row, decode_extra
from .mail import MAIL_KIND, parse_addresses

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
    # write-time enrichment: decoded out of the sealed meta so a
    # reader never has to know which dict key holds which label
    urgency: int = 0
    needs_reply: bool = False
    noise: bool = False
    deadline_at: datetime | None = None
    summary: str = ""
    entity_ids: tuple[int, ...] = ()


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
        entities: Entities | None = None,
        commitments: Commitments | None = None,
        enricher: Enricher | None = None,
        facts: Facts | None = None,
        enrichment: bool = False,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._contacts = contacts
        self._indexer = indexer
        self._index = index
        self._cold_batch = cold_batch
        self._entities = entities
        self._commitments = commitments
        self._enricher = enricher
        self._facts = facts
        self._enrichment = enrichment
        # the proactive path (watches + reminders) joins here after the
        # agent exists, because an alert is delivered through the loop
        self._proactive: Any = None
        # background enrichment in flight — tracked so shutdown and tests
        # never assert on work still mid-air
        self._enriching: set[asyncio.Task[None]] = set()
        # One lock for every sealed-meta merge. Salience and enrichment are
        # both read-modify-write on the same blob, and two spawned writers
        # racing would silently drop whichever finished first. Writes are a
        # couple per event, so one lock costs nothing worth measuring.
        self._meta_lock = asyncio.Lock()

    async def wait_idle(self) -> None:
        """Every enrichment currently in flight.
        A watcher evaluation rides the same task, so waiting here also
        waits out the alert an event owes."""
        while self._enriching:
            await asyncio.gather(*self._enriching, return_exceptions=True)

    def attach_proactive(self, proactive: Any) -> None:
        """Join the proactive path (watches, urgent alerts) to the store.

        The store is the one place every channel funnels an event through,
        so the evaluator lives on the same task that already links
        addresses and classifies: an alert never delays ingest, and
        ``wait_idle`` covers it."""
        self._proactive = proactive

    def attach_enrichment(
        self,
        *,
        entities: Entities | None = None,
        commitments: Commitments | None = None,
        enricher: Enricher | None = None,
        facts: Facts | None = None,
        enrichment: bool = False,
    ) -> None:
        """Join the write-time enrichment pieces to the store.

        The store is built before the identity layer, which needs the model
        registry, which needs the connectors — so the two are wired
        together here rather than by reordering boot. Until this is called,
        ingest stores exactly what it stored before: the floor still runs
        (it needs nothing), but nothing is linked and nothing is classified.
        """
        self._entities = entities
        self._commitments = commitments
        self._enricher = enricher
        self._facts = facts
        self._enrichment = enrichment

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
        dedup_key: str | None = None,
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
        extra = dict(meta or {})
        # The deterministic floor runs here, synchronously, before anything
        # is written: whatever else is late — the classifier, the
        # embedder, the network — an event whose own words say "urgent" is
        # never invisible. It is not behind the enrichment flag because the
        # one thing that must not fail is the one thing that cannot fail.
        floor = heuristic_floor(enrichment_text(kind, stored_payload))
        if floor:
            extra["urgency"] = max(int(extra.get("urgency") or 0), floor)
            extra["urgency_floor"] = True
        sealed_meta = {
            "source": source,
            "kind": kind,
            "occurred_at": when.isoformat(),
            "salience": 0.0,
            "memorable": False,
            "extra": extra,
        }
        if dedup_key is not None:
            # the source's own identity for this thing (a provider message
            # id), tagged on its own instead of over the payload — a refetch
            # that changed the body is still the same message, and the poll
            # and the tool path agree on it. The key is hashed into the tag
            # and never stored; only the HMAC reaches the column.
            dedup_tag = self._cipher.dedup_mac(b"dedup:" + dedup_key.encode("utf-8"))
        else:
            dedup_tag = self._cipher.dedup_mac(canonical_payload(kind, stored_payload))

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
                dedup_tag,
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
                    meta=extra,
                    **decode_extra(extra),
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
                    when,
                )
            except Exception:
                log.warning(
                    "indexing failed for event %s — backfill will retry", event_id, exc_info=True
                )
        if (
            self._entities is not None
            or (self._enricher is not None and self._enrichment)
            or self._proactive is not None
        ):
            self._spawn_enrichment(event_id, kind, stored_payload, when)
        return IngestResult(stored=True, reason="new", event_id=event_id)

    # -- write-time enrichment -----------------------------------------------

    def _spawn_enrichment(
        self, event_id: int, kind: str, payload: dict[str, Any], occurred_at: datetime
    ) -> None:
        """Start the slow half of enrichment without making ingest wait.

        Linking addresses needs the database and classifying needs a model
        turn; neither belongs in front of a reply. The event is already
        stored and already floor-scored, so this task only adds to what is
        known — a failure costs detail, never the event.
        """
        task = asyncio.create_task(self._enrich(event_id, kind, payload, occurred_at))
        self._enriching.add(task)
        task.add_done_callback(self._enriching.discard)

    async def _enrich(
        self, event_id: int, kind: str, payload: dict[str, Any], occurred_at: datetime
    ) -> None:
        extra: dict[str, Any] = {}
        trusted = True
        if self._entities is not None:
            try:
                extra.update(await self._link_addresses(event_id, kind, payload))
            except Exception:
                log.warning("entity linking failed for event %s", event_id, exc_info=True)
        if self._entities is not None and kind == MAIL_KIND:
            # a mail from someone other than the owner is untrusted content:
            # it can inform recall, but it cannot establish a reserved fact
            try:
                trusted = await self._mail_is_trusted(payload)
            except Exception:
                trusted = False
        if self._enricher is not None and self._enrichment:
            try:
                result = await self._enricher.enrich(
                    enrichment_text(kind, payload), occurred_at=occurred_at
                )
            except Exception:
                log.warning("enrichment failed for event %s", event_id, exc_info=True)
                result = None
            if result is not None:
                extra.update(result.to_extra())
                if self._commitments is not None and result.commitments:
                    await self._commitments.add_many(
                        [
                            {
                                "kind": commitment.kind,
                                "text": commitment.text,
                                "due_at": commitment.due_at,
                            }
                            for commitment in result.commitments
                        ],
                        source_event_id=event_id,
                        counterparty_identity_id=_first(extra.get("entities")),
                        urgency=result.urgency,
                        thread_id=str(payload.get("thread_id") or ""),
                        document=_document_name(payload),
                        media_id=_int_or_none(payload.get("media_id")),
                    )
                if self._facts is not None and result.facts:
                    await self._consolidate_facts(result.facts, event_id, trusted)
                if result.noise and self._indexer is not None:
                    # a noise event keeps its one-line summary instead of a
                    # chunk per paragraph, so it stops winning retrieval
                    try:
                        await self._indexer.replace_with_summary(
                            event_id, result.summary or str(payload.get("subject") or "")
                        )
                    except Exception:
                        log.warning("could not collapse noise event %s", event_id, exc_info=True)
        if extra:
            await self.merge_meta(event_id, extra)
        if self._proactive is not None:
            # the alert rides this same tracked task, so it never sits in
            # front of a reply and `wait_idle` can wait it out
            try:
                event = await self.get(event_id)
                if event is not None:
                    await self._proactive.evaluate_event(event)
            except Exception:
                log.warning("proactive evaluation failed for event %s", event_id, exc_info=True)

    async def _link_addresses(
        self, event_id: int, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Link every address an event carries to an identity.

        Fill the event's own envelope from what the mail carried, and never
        from a guess: an unknown display name that looks like the owner's
        produces a question, not a merge.
        """
        if kind != MAIL_KIND:
            return {}
        assert self._entities is not None
        seen: list[int] = []
        for field in ("sender", "to", "cc"):
            for address in parse_addresses(payload.get(field)):
                linked = await self._entities.link_address("email", address.address, address.name)
                if linked is None:
                    continue
                if linked.entity.id not in seen:
                    seen.append(linked.entity.id)
                if linked.alias_candidate and linked.alias_owner_id is not None:
                    # ask once, and only on a later human turn
                    await self._entities.ask_alias_once(
                        linked.entity.id,
                        address=address.address,
                        name=address.name,
                        owner_name=str(linked.alias_owner_id),
                    )
        if not seen:
            return {}
        return {"entities": seen}

    async def _mail_is_trusted(self, payload: dict[str, Any]) -> bool:
        """True when the mail came from one of the owner's own addresses.

        A mail from anyone else is untrusted content: it may be recalled,
        but it cannot establish a reserved fact (identity, contacts,
        policy, watches) on its own.
        """
        assert self._entities is not None
        addresses = parse_addresses(payload.get("sender"))
        if not addresses:
            return True
        for address in addresses:
            if await self._entities.is_owner_address("email", address.address):
                return True
        return False

    async def _consolidate_facts(
        self, candidates: list[ExtractedFact], event_id: int, trusted: bool
    ) -> None:
        """Fold the classifier's candidate claims into the facts layer.

        One bad candidate never costs the others, and the event id is the
        mandatory provenance. The subject resolves to the owner's identity
        when the claim is about the user, and otherwise stays a label in the
        fact's own sealed body.
        """
        assert self._facts is not None
        for candidate in candidates:
            try:
                subject_id = await self._fact_subject_identity(candidate.subject)
                await self._facts.consolidate(
                    subject_identity_id=subject_id,
                    subject=candidate.subject,
                    key=candidate.key,
                    value=candidate.value,
                    type=candidate.type,
                    provenance=[event_id],
                    trusted=trusted,
                )
            except Exception:
                log.warning("could not consolidate a fact from event %s", event_id)

    async def _fact_subject_identity(self, subject: str) -> int | None:
        """The owner's identity when the claim is about the user, else None."""
        if self._entities is None:
            return None
        label = " ".join((subject or "").split()).lower()
        if label not in {"user", "me", "i", "owner", "myself"}:
            return None
        try:
            owner = await self._entities.owner_entity()
        except Exception:
            return None
        return owner.id if owner is not None else None

    async def merge_meta(
        self,
        event_id: int,
        extra: dict[str, Any],
        *,
        salience: float | None = None,
        memorable: bool | None = None,
    ) -> None:
        """Fold fields into one event's sealed meta, under one lock.

        Salience and enrichment both read-modify-write that single blob, so
        they share this method rather than each re-implementing the merge —
        otherwise whichever writer finished second would silently erase the
        other's fields.
        """
        async with self._meta_lock:
            current = await self.get(event_id)
            if current is None:
                return
            merged = {**current.meta, **extra}
            score = current.salience_score if salience is None else float(salience)
            keep = current.memorable if memorable is None else bool(memorable)
            sealed_meta = {
                "source": current.source,
                "kind": current.kind,
                "occurred_at": current.occurred_at.isoformat(),
                "salience": score,
                "memorable": keep,
                "extra": merged,
            }
            await self._pool.execute(
                "UPDATE events SET meta_enc = $1, key_version = $2 WHERE id = $3",
                self._cipher.encrypt_json(sealed_meta, aad=f"events:meta_enc:{event_id}"),
                self._cipher.key_id,
                event_id,
            )
            if self._index is not None:
                self._index.update_salience(event_id, score, keep, merged)

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
            if event.source == source and event.occurred_at >= since:
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
        await self.merge_meta(event_id, extra, salience=score, memorable=memorable)

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
            urgency=event.urgency,
            needs_reply=event.needs_reply,
            noise=event.noise,
            deadline_at=event.deadline_at,
            summary=event.summary,
            entity_ids=event.entity_ids,
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


def _first(values: Any) -> int | None:
    """The first identity an event was linked to — the counterparty a
    commitment belongs to, when the event names one."""
    if isinstance(values, (list, tuple)) and values:
        first = values[0]
        return int(first) if isinstance(first, int) else None
    return None


def _document_name(payload: dict[str, Any]) -> str:
    """The document a commitment can be linked to, when the event names one."""
    for key in ("filename", "document", "doc"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
