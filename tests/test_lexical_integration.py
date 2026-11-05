"""The lexical index against the real sealed store, on the scratch database.

The pure BM25 behaviour is in test_query_pipeline.py; these prove the two
halves stay joined in production: the chunk writer feeds the index (one
writer, no drift) and a restart rebuilds the same answers from the sealed
rows without ever writing plaintext back.
"""

from __future__ import annotations

import pytest
from fakes import FakeEmbedder

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import EventStore
from aether.memory.lexical import LexicalIndex
from aether.memory.vectors import VectorMemory


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def _listener(index: LexicalIndex):
    def apply(chunk: dict) -> None:
        if chunk.get("action") == "remove":
            index.remove_event(int(chunk["event_id"]))
            return
        index.add_chunk(
            chunk_id=int(chunk["chunk_id"]),
            event_id=int(chunk["event_id"]),
            ordinal=int(chunk.get("ordinal") or 0),
            source=str(chunk.get("source", "")),
            kind=str(chunk.get("kind", "")),
            filename=str(chunk.get("filename", "")),
            location=str(chunk.get("location", "")),
            occurred_at=chunk.get("occurred_at"),
            text=str(chunk.get("text", "")),
        )

    return apply


@pytest.mark.integration
async def test_the_chunk_writer_feeds_the_lexical_index(db) -> None:
    cipher = _cipher()
    vectors = VectorMemory(db, cipher, FakeEmbedder())
    lexical = LexicalIndex()
    vectors.add_chunk_listener(_listener(lexical))
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig(), indexer=vectors)

    result = await events.ingest(
        source="telegram",
        kind="chat_message",
        payload={"text": "the quarterly invoice needs approval before Friday"},
    )
    assert result.stored
    await vectors.wait_idle()

    hits = lexical.search("invoice approval")
    assert hits and hits[0].event_id == result.event_id
    assert "invoice" in hits[0].text


@pytest.mark.integration
async def test_a_restart_rebuilds_the_index_from_sealed_chunks(db) -> None:
    cipher = _cipher()
    vectors = VectorMemory(db, cipher, FakeEmbedder())
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig(), indexer=vectors)

    stored = await events.ingest(
        source="telegram",
        kind="chat_message",
        payload={"text": "the design review moved to Tuesday afternoon"},
    )
    assert stored.stored
    await vectors.wait_idle()

    rebuilt = LexicalIndex()
    loaded = await rebuilt.rebuild(db, cipher)

    assert loaded >= 1
    hits = rebuilt.search("design review")
    assert hits and hits[0].event_id == stored.event_id
    # the index holds decrypted text in memory only; the row stays sealed
    row = await db.fetchrow(
        "SELECT text_enc FROM memory_chunks WHERE event_id = $1", stored.event_id
    )
    assert row is not None
    assert b"design review" not in row["text_enc"]


@pytest.mark.integration
async def test_noise_collapse_replaces_the_indexed_chunks(db) -> None:
    cipher = _cipher()
    vectors = VectorMemory(db, cipher, FakeEmbedder())
    lexical = LexicalIndex()
    vectors.add_chunk_listener(_listener(lexical))
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig(), indexer=vectors)

    stored = await events.ingest(
        source="telegram",
        kind="chat_message",
        payload={"text": "weekly newsletter unsubscribe sale discount coupon " * 40},
    )
    assert stored.stored
    await vectors.wait_idle()
    assert lexical.search("unsubscribe")

    await vectors.replace_with_summary(stored.event_id, "A weekly newsletter summary")

    assert lexical.search("unsubscribe") == []
    hits = lexical.search("newsletter summary")
    assert hits and hits[0].event_id == stored.event_id
