"""Inspection and forgetting (plan.md section 12.12).

Forgetting has to be real: the row, its chunks, the resident index, the
lexical index, the promises it produced and the claims it supported all go,
and a tombstone keeps the next poll from storing the same message again.
Everything runs against a real database, because the cascade, the sealed
rows and the audit chain are the point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fakes import FakeEmbedder

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig, MemoryConfig
from aether.memory.commitments import CANCELLED, Commitments
from aether.memory.control import MemoryControl, describe_event
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import Event, EventStore
from aether.memory.facts import FORGOTTEN, Facts
from aether.memory.indexes import MemoryIndex
from aether.memory.lexical import LexicalIndex
from aether.memory.summaries import DAILY, Summaries
from aether.memory.vectors import VectorMemory

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _mail(**overrides) -> dict:
    payload = {
        "message_id": "m1",
        "thread_id": "t1",
        "subject": "Invoice for October",
        "sender": "Rahul Sharma <rahul@example.com>",
        "to": "me@example.com",
        "cc": "",
        "date": "Tue, 03 Nov 2026 11:46:00 +0530",
        "body": "Please find the invoice attached. It is due on Friday.",
    }
    payload.update(overrides)
    return payload


async def _world(db):
    """The real stores over one fresh key, wired the way boot wires them."""
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    index = MemoryIndex()
    lexical = LexicalIndex(hot_chunks=100)
    vectors = VectorMemory(db, cipher, FakeEmbedder())

    def listen(chunk: dict) -> None:
        if chunk.get("action") == "remove":
            lexical.remove_event(int(chunk["event_id"]))
            return
        lexical.add_chunk(
            chunk_id=int(chunk["chunk_id"]),
            event_id=int(chunk["event_id"]),
            ordinal=int(chunk.get("ordinal") or 0),
            source=str(chunk.get("source", "")),
            kind=str(chunk.get("kind", "")),
            filename=str(chunk.get("filename", "")),
            location=str(chunk.get("location", "")),
            occurred_at=chunk.get("occurred_at") or NOW,
            text=str(chunk.get("text", "")),
        )

    vectors.add_chunk_listener(listen)
    events = EventStore(db, cipher, audit, ContactsConfig(), indexer=vectors, index=index)
    facts = Facts(db, cipher, audit)
    commitments = Commitments(db, cipher, audit)
    summaries = Summaries(db, cipher)
    control = MemoryControl(
        pool=db,
        cipher=cipher,
        audit=audit,
        events=events,
        index=index,
        lexical=lexical,
        vectors=vectors,
        facts=facts,
        commitments=commitments,
        summaries=summaries,
        memory=MemoryConfig(),
    )
    return SimpleNamespace(
        events=events,
        facts=facts,
        commitments=commitments,
        summaries=summaries,
        index=index,
        lexical=lexical,
        vectors=vectors,
        control=control,
        audit=audit,
    )


async def _ingest(world, dedup_key: str = "m1"):
    result = await world.events.ingest(
        source="gmail",
        kind="mail",
        payload=_mail(),
        dedup_key=dedup_key,
        occurred_at=NOW,
    )
    assert result.stored
    await world.vectors.wait_idle()
    return result.event_id


# ---------------------------------------------------------------------------
# the plain sentence
# ---------------------------------------------------------------------------


def test_describe_event_speaks_plainly() -> None:
    mail = Event(
        id=1,
        source="gmail",
        kind="mail",
        occurred_at=NOW,
        payload=_mail(),
        salience_score=0.0,
        memorable=False,
        meta={},
    )
    assert describe_event(mail) == "an email from Rahul Sharma about Invoice for October"

    chat = Event(
        id=2,
        source="telegram",
        kind="chat_message",
        occurred_at=NOW,
        payload={"text": "call the plumber before friday"},
        salience_score=0.0,
        memorable=False,
        meta={},
    )
    assert "call the plumber" in describe_event(chat)

    doc = Event(
        id=3,
        source="telegram",
        kind="shared_doc",
        occurred_at=NOW,
        payload={"filename": "OSY.pdf"},
        salience_score=0.0,
        memorable=False,
        meta={},
    )
    assert describe_event(doc) == "the document OSY.pdf"

    # never a JSON blob, even for a shape nothing knows
    unknown = Event(
        id=4,
        source="x",
        kind="mystery",
        occurred_at=NOW,
        payload={"a": 1},
        salience_score=0.0,
        memorable=False,
        meta={},
    )
    assert describe_event(unknown) == "a remembered item"


# ---------------------------------------------------------------------------
# forgetting one item
# ---------------------------------------------------------------------------


async def test_forgetting_removes_the_row_chunks_indexes_and_claims(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)
    assert world.index.get(event_id) is not None
    assert world.lexical.search("invoice")
    fact = await world.facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="invoice_due",
        value="Friday",
        provenance=[event_id],
        now=NOW,
    )
    assert fact.fact is not None

    report = await world.control.forget_event(event_id)

    assert report.found
    assert report.label == "Forgot an email from Rahul Sharma about Invoice for October."
    assert await world.events.get(event_id) is None
    assert world.index.get(event_id) is None
    assert world.lexical.search("invoice") == []
    assert await db.fetchval("SELECT count(*) FROM memory_chunks") == 0
    assert await db.fetchval("SELECT count(*) FROM events") == 0
    assert await world.facts.active() == []

    row = await db.fetchrow(
        "SELECT tool_name, decision, outcome FROM audit_log ORDER BY seq DESC LIMIT 1"
    )
    assert row["tool_name"] == "memory.forget"
    assert row["decision"] == "allow"
    assert "forgot one item" in row["outcome"]


async def test_a_forgotten_message_cannot_be_ingested_again(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world, dedup_key="m1")
    await world.control.forget_event(event_id)

    again = await world.events.ingest(
        source="gmail", kind="mail", payload=_mail(), dedup_key="m1", occurred_at=NOW
    )

    assert again.stored is False
    assert again.reason == "forgotten"
    assert await db.fetchval("SELECT count(*) FROM events") == 0
    assert await db.fetchval("SELECT count(*) FROM forgotten_events") == 1


async def test_forgetting_cancels_the_promise_the_item_produced(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)
    commitment = await world.commitments.add(
        kind="task",
        text="send the invoice",
        source_event_id=event_id,
    )

    report = await world.control.forget_event(event_id)

    assert report.counts["commitments"] == 1
    stored = await world.commitments.get(commitment.id)
    assert stored is not None and stored.status == CANCELLED


async def test_a_second_forget_is_a_quiet_noop(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)

    assert (await world.control.forget_event(event_id)).found
    second = await world.control.forget_event(event_id)

    assert second.found is False
    assert second.kind == ""


# ---------------------------------------------------------------------------
# forgetting one claim, promise or summary
# ---------------------------------------------------------------------------


async def test_forgetting_a_claim_ends_it_without_deleting_the_history(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)
    outcome = await world.facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="favourite_tea",
        value="masala chai",
        type="preference",
        provenance=[event_id],
        now=NOW,
    )
    assert outcome.fact is not None

    report = await world.control.forget_fact(outcome.fact.id)

    assert report.found
    assert await world.facts.active() == []
    stored = await world.facts.get(outcome.fact.id)
    assert stored is not None
    assert stored.status == FORGOTTEN
    # the words survive; only the claim's life ended
    assert stored.value == "masala chai"


async def test_forgetting_a_promise_and_a_summary(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)
    commitment = await world.commitments.add(kind="promise", text="send the deck")
    summary = await world.summaries.upsert(DAILY, "2026-11-03", "A quiet day.", [event_id])

    promise_report = await world.control.forget_commitment(commitment.id)
    summary_report = await world.control.forget_summary(summary.id)

    assert promise_report.found and summary_report.found
    stored = await world.commitments.get(commitment.id)
    assert stored is not None and stored.status == CANCELLED
    assert await world.summaries.by_id(summary.id) is None


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------


async def test_the_overview_counts_what_is_actually_there(db) -> None:
    world = await _world(db)
    await _ingest(world)

    overview = await world.control.overview()

    assert overview["resident_events"] == 1
    assert overview["indexed_chunks"] >= 1
    assert overview["active_facts"] == 0
    assert overview["open_commitments"] == 0
    assert overview["semantic_search"]["on"] is True
    assert overview["flags"]["enrichment"] is True


async def test_items_read_as_sentences_and_search_finds_them(db) -> None:
    world = await _world(db)
    event_id = await _ingest(world)

    recent = await world.control.items()
    assert [item["id"] for item in recent] == [event_id]
    assert recent[0]["plain"] == "an email from Rahul Sharma about Invoice for October"
    assert "{" not in recent[0]["plain"]

    found = await world.control.items(query="invoice")
    assert [item["id"] for item in found] == [event_id]
    assert await world.control.items(query="quantum computing") == []
