"""The resident event index: boot rebuild, time/count queries, cold scans,
and the sealed write path that keeps it current."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import EventStore
from aether.memory.indexes import MemoryIndex


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def _at(hour: int) -> datetime:
    return datetime(2026, 11, 3, hour, 0, tzinfo=UTC)


@pytest.mark.integration
async def test_ingest_writes_the_sealed_copy(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    result = await store.ingest(
        source="gmail",
        kind="message",
        payload={"text": "the invoice is due", "subject": "bill"},
        occurred_at=_at(9),
        meta={"label": "mail"},
    )
    assert result.event_id is not None
    row = await db.fetchrow(
        "SELECT source_token, dedup_mac, meta_enc FROM events WHERE id = $1", result.event_id
    )
    assert row["source_token"] == cipher.blind_token("source", "gmail")
    assert row["dedup_mac"] is not None
    meta = cipher.decrypt_json(row["meta_enc"], aad=f"events:meta_enc:{result.event_id}")
    assert meta["source"] == "gmail"
    assert meta["kind"] == "message"
    assert meta["occurred_at"].startswith("2026-11-03T09:00")
    assert meta["extra"] == {"label": "mail"}

    event = await store.get(result.event_id)
    assert event is not None
    assert event.payload["subject"] == "bill"
    assert event.meta == {"label": "mail"}


@pytest.mark.integration
async def test_rebuild_restores_time_and_count_views(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    first = await store.ingest(
        source="gmail", kind="message", payload={"text": "one"}, occurred_at=_at(8)
    )
    second = await store.ingest(
        source="telegram", kind="chat_message", payload={"text": "two"}, occurred_at=_at(10)
    )
    assert first.event_id is not None and second.event_id is not None
    await store.update_salience(first.event_id, 8.0, True, category="work")

    index = MemoryIndex()
    assert await index.rebuild(db, cipher, hot_limit=50) == 2
    assert index.max_id == second.event_id
    assert [event.id for event in index.recent(10)] == [second.event_id, first.event_id]
    memorable = index.recent_memorable(10)
    assert [event.id for event in memorable] == [first.event_id]
    assert memorable[0].salience_score == 8.0
    assert index.count_since("gmail", _at(0)) == 1
    assert index.count_memorable_since(_at(0)) == 1
    assert index.covers(_at(9))

    wired = EventStore(db, cipher, AuditLog(db), ContactsConfig(), index=index, cold_batch=10)
    got = await wired.get(first.event_id)
    assert got is not None and got.meta["category"] == "work"
    assert [event.id for event in await wired.recent_memorable(10)] == [first.event_id]
    assert await wired.count_recent("gmail", hours=48) == 1


@pytest.mark.integration
async def test_cold_scan_reaches_older_history(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    for hour in (6, 7, 8):
        await store.ingest(
            source="gmail", kind="message", payload={"text": f"mail {hour}"}, occurred_at=_at(hour)
        )
    index = MemoryIndex()
    assert await index.rebuild(db, cipher, hot_limit=1) == 1
    assert not index.covers(_at(6))
    await index.ensure_since(db, cipher, _at(6), batch=1)
    assert index.covers(_at(6))
    assert len(index.since_time(_at(6))) == 3


@pytest.mark.integration
async def test_cold_scan_survives_an_unreadable_event(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    for hour in (6, 8):
        await store.ingest(
            source="gmail", kind="message", payload={"text": f"mail {hour}"}, occurred_at=_at(hour)
        )
    oldest = await db.fetchval("SELECT MIN(id) FROM events")
    await db.execute("UPDATE events SET meta_enc = $1 WHERE id = $2", b"not sealed", oldest)

    index = MemoryIndex()
    assert await index.rebuild(db, cipher, hot_limit=1) == 1
    # the cold scan must skip the bad row, not raise out of ensure_since
    await index.ensure_since(db, cipher, _at(0), batch=1, max_batches=4)
    assert index.skipped == 1
    assert index.covers(_at(0))
    assert [event.occurred_at for event in index.since_time(_at(0))] == [_at(8)]


@pytest.mark.integration
async def test_salience_write_updates_the_index(db) -> None:
    cipher = _cipher()
    index = MemoryIndex()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig(), index=index)
    result = await store.ingest(
        source="gmail", kind="message", payload={"text": "urgent"}, occurred_at=_at(9)
    )
    assert result.event_id is not None
    await store.update_salience(result.event_id, 9.0, True, actionability="reply")
    indexed = index.get(result.event_id)
    assert indexed is not None
    assert indexed.memorable and indexed.salience_score == 9.0
    # the salience fields land, and the floor's own urgency survives the
    # merge — the salience judge must not be able to erase "urgent"
    assert indexed.meta["actionability"] == "reply"
    assert indexed.urgency == 2
    assert indexed.meta["urgency_floor"] is True


@pytest.mark.integration
async def test_dedup_still_holds_after_the_sealed_write(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    first = await store.ingest(source="gmail", kind="message", payload={"text": "same"})
    second = await store.ingest(source="gmail", kind="message", payload={"text": "same"})
    other = await store.ingest(source="telegram", kind="message", payload={"text": "same"})
    assert first.stored and not second.stored
    assert other.stored  # a different source is a different dedup space
    assert await db.fetchval("SELECT COUNT(*) FROM events") == 2


@pytest.mark.integration
async def test_recent_hours_cold_scans_before_filtering(db) -> None:
    cipher = _cipher()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    old = await store.ingest(
        source="gmail",
        kind="message",
        payload={"text": "old"},
        occurred_at=datetime.now(UTC) - timedelta(hours=30),
    )
    assert old.event_id is not None
    await store.update_salience(old.event_id, 7.0, True)
    index = MemoryIndex()
    await index.rebuild(db, cipher, hot_limit=1)
    wired = EventStore(db, cipher, AuditLog(db), ContactsConfig(), index=index)
    assert [event.id for event in await wired.recent_memorable(10, hours=48)] == [old.event_id]
    assert await wired.recent_memorable(10, hours=1) == []
