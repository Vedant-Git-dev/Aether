"""The seal migration: fills sealed columns, verifies round trips, reruns
as a no-op, and leaves an unreadable row alone instead of inventing one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakePersonJudge

from aether.agent.traces import Traces
from aether.authz.approvals import Approvals
from aether.authz.audit import AuditLog
from aether.chat.ws import ChatHistory
from aether.config import ContactsConfig, MediaConfig
from aether.media.store import MediaStore
from aether.memory.crypto import ENVELOPE_MAGIC, Cipher, generate_key_b64
from aether.memory.db import run_migrations
from aether.memory.entities import Entities, normalize_handle
from aether.memory.events import EventStore
from aether.memory.seal_migrate import backfill, pending_counts
from aether.proactive.store import ProactiveStore
from aether.routines.store import Routines
from aether.scheduler.jobs import Scheduler


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


async def _seed(db, cipher: Cipher) -> dict[str, int]:
    """One legacy row in every table the sealer touches."""
    audit = AuditLog(db)
    events = EventStore(db, cipher, audit, ContactsConfig())
    ingested = await events.ingest(
        source="gmail",
        kind="message",
        payload={"text": "the invoice is due tomorrow", "subject": "bill"},
        occurred_at=datetime(2026, 11, 3, 9, 30, tzinfo=UTC),
    )
    assert ingested.event_id is not None
    event_id = ingested.event_id

    entities = Entities(db, cipher, audit, FakePersonJudge([]))
    person = await entities.resolve("gmail", "friend@example.com", "Friend")
    handle_id = await db.fetchval(
        "SELECT id FROM identity_handles WHERE identity_id = $1", person.id
    )

    approvals = Approvals(db, cipher, audit)
    approval = await approvals.create(
        tool_name="gmail__send",
        params={"to": "friend@example.com"},
        rules_matched="user rule 1",
        note="replying to the invoice mail",
    )

    scheduler = Scheduler(db, cipher, audit)
    scheduled = await scheduler.create(
        label="send the invoice",
        run_at=datetime.now(UTC) + timedelta(hours=1),
        payload={"type": "tool", "tool": "gmail__send", "params": {}},
    )

    routines = Routines(db, cipher, audit)
    routine = await routines.create(
        label="when mail arrives",
        trigger={"source": "gmail"},
        action={"tool": "gmail__list"},
    )

    traces = Traces(db, cipher)
    trace = await traces.create(kind="turn", label="what is due", payload={"turn": "abc"})

    proactive = ProactiveStore(db, cipher, audit)
    task = await proactive.claim(
        skill="followups",
        key="thread-1",
        label="follow up on: invoice",
        opportunity={"thread_id": "thread-1"},
        day="2026-11-03",
    )
    assert task is not None

    media = MediaStore(db, cipher, MediaConfig())
    blob = await media.put(
        b"\x89PNG\r\n\x1a\nnot-really-a-png", filename="budget.png", origin="telegram", width=10
    )

    chat = ChatHistory(db, cipher)
    await chat.append("telegram", "in", "hello")

    # a chunk with a legacy plaintext vector, as the pre-encryption code wrote
    chunk_id = await db.fetchval(
        "INSERT INTO memory_chunks (event_id, source, kind, filename, location,"
        " text_enc, embedding, embed_model, occurred_at)"
        " VALUES ($1, 'gmail', 'shared_doc', 'invoice.pdf', 'page 2', $2, $3, 'fake', $4)"
        " RETURNING id",
        event_id,
        cipher.encrypt_text("page two text", aad="chunks:text_enc:placeholder"),
        [0.25, 0.5],
        datetime(2026, 11, 3, 9, 31, tzinfo=UTC),
    )
    # the AAD needs the real id; rewrite the text with the right binding
    await db.execute(
        "UPDATE memory_chunks SET text_enc = $1 WHERE id = $2",
        cipher.encrypt_text("page two text", aad=f"chunks:text_enc:{chunk_id}"),
        chunk_id,
    )

    # the stores sealed their new rows; strip the sealed copies so this test
    # exercises the sealer's legacy path in every table, as a real pre-phase
    # database would
    await db.execute(
        "UPDATE events SET meta_enc = NULL, source_token = NULL, dedup_mac = NULL WHERE id = $1",
        event_id,
    )
    await db.execute("UPDATE identities SET profile_enc = NULL WHERE id = $1", person.id)
    await db.execute(
        "UPDATE identity_handles SET handle_enc = NULL, platform_token = NULL,"
        " normalized_token = NULL WHERE identity_id = $1",
        person.id,
    )
    await db.execute(
        "UPDATE pending_approvals SET rules_matched_enc = NULL, note_enc = NULL WHERE id = $1",
        approval.id,
    )
    await db.execute("UPDATE scheduled_actions SET meta_enc = NULL WHERE id = $1", scheduled.id)
    await db.execute("UPDATE routines SET label_enc = NULL WHERE id = $1", routine.id)
    await db.execute("UPDATE decision_traces SET label_enc = NULL WHERE id = $1", trace.id)
    await db.execute(
        "UPDATE proactive_tasks SET label_enc = NULL, meta_enc = NULL,"
        " opportunity_token = NULL, day_token = NULL WHERE id = $1",
        task.id,
    )
    await db.execute(
        "UPDATE media_blobs SET meta_enc = NULL, origin_token = NULL WHERE id = $1",
        blob.media_id,
    )
    await db.execute("UPDATE chat_messages SET surface_token = NULL")
    return {
        "event": event_id,
        "identity": person.id,
        "handle": person.handles[0][1],
        "handle_id": handle_id,
        "approval": approval.id,
        "scheduled": scheduled.id,
        "routine": routine.id,
        "trace": trace.id,
        "task": task.id,
        "media": blob.media_id,
        "chunk": chunk_id,
    }


@pytest.mark.integration
async def test_seal_backfill_fills_every_table_and_verifies(db) -> None:
    cipher = _cipher()
    ids = await _seed(db, cipher)
    results = await backfill(db, cipher, batch=50, sleep=0)
    assert all(result.remaining == 0 for result in results), results
    assert all(result.failed == 0 for result in results), results
    assert sum(result.sealed for result in results) >= 9

    event = await db.fetchrow(
        "SELECT source_token, dedup_mac, meta_enc FROM events WHERE id = $1", ids["event"]
    )
    assert event["source_token"] is not None and event["dedup_mac"] is not None
    meta = cipher.decrypt_json(event["meta_enc"], aad=f"events:meta_enc:{ids['event']}")
    assert meta["source"] == "gmail"
    assert meta["kind"] == "message"
    assert meta["occurred_at"].startswith("2026-11-03T09:30")
    assert meta["extra"] == {}

    profile = await db.fetchrow("SELECT profile_enc FROM identities WHERE id = $1", ids["identity"])
    assert (
        cipher.decrypt_json(
            profile["profile_enc"], aad=f"identities:profile_enc:{ids['identity']}"
        )["display_name"]
        == "Friend"
    )

    handle = await db.fetchrow(
        "SELECT handle_enc, platform_token, normalized_token FROM identity_handles"
        " WHERE identity_id = $1",
        ids["identity"],
    )
    assert cipher.decrypt_json(
        handle["handle_enc"], aad=f"identity_handles:handle_enc:{ids['handle_id']}"
    ) == {"platform": "gmail", "handle": ids["handle"]}
    assert handle["platform_token"] == cipher.blind_token("platform", "gmail")
    assert handle["normalized_token"] == cipher.blind_token(
        "handle", normalize_handle("gmail", ids["handle"])
    )

    approval = await db.fetchrow(
        "SELECT rules_matched_enc, note_enc FROM pending_approvals WHERE id = $1",
        ids["approval"],
    )
    assert (
        cipher.decrypt_text(
            approval["rules_matched_enc"],
            aad=f"pending_approvals:rules_matched_enc:{ids['approval']}",
        )
        == "user rule 1"
    )
    assert (
        cipher.decrypt_text(
            approval["note_enc"], aad=f"pending_approvals:note_enc:{ids['approval']}"
        )
        == "replying to the invoice mail"
    )

    scheduled = await db.fetchrow(
        "SELECT meta_enc FROM scheduled_actions WHERE id = $1", ids["scheduled"]
    )
    schedule_meta = cipher.decrypt_json(
        scheduled["meta_enc"], aad=f"scheduled_actions:meta_enc:{ids['scheduled']}"
    )
    assert schedule_meta["label"] == "send the invoice"
    assert "run_at" in schedule_meta

    routine = await db.fetchrow("SELECT label_enc FROM routines WHERE id = $1", ids["routine"])
    assert (
        cipher.decrypt_text(routine["label_enc"], aad=f"routines:label_enc:{ids['routine']}")
        == "when mail arrives"
    )
    trace = await db.fetchrow("SELECT label_enc FROM decision_traces WHERE id = $1", ids["trace"])
    assert (
        cipher.decrypt_text(trace["label_enc"], aad=f"decision_traces:label_enc:{ids['trace']}")
        == "what is due"
    )

    task = await db.fetchrow(
        "SELECT label_enc, opportunity_token, day_token FROM proactive_tasks WHERE id = $1",
        ids["task"],
    )
    assert (
        cipher.decrypt_text(task["label_enc"], aad=f"proactive_tasks:label_enc:{ids['task']}")
        == "follow up on: invoice"
    )
    assert task["opportunity_token"] == cipher.blind_token("opportunity", "followups\x1fthread-1")
    assert task["day_token"] == cipher.blind_token("day", "2026-11-03")

    media = await db.fetchrow(
        "SELECT meta_enc, origin_token FROM media_blobs WHERE id = $1", ids["media"]
    )
    assert (
        cipher.decrypt_json(media["meta_enc"], aad=f"media_blobs:meta_enc:{ids['media']}")[
            "filename"
        ]
        == "budget.png"
    )
    assert media["origin_token"] == cipher.blind_token("source", "telegram")

    chat = await db.fetchrow("SELECT surface_token FROM chat_messages LIMIT 1")
    assert chat["surface_token"] == cipher.blind_token("source", "telegram")

    chunk = await db.fetchrow(
        "SELECT meta_enc, dims, embedding_enc, embedding FROM memory_chunks WHERE id = $1",
        ids["chunk"],
    )
    assert chunk["dims"] == 2
    assert chunk["embedding"] is None
    assert chunk["embedding_enc"].startswith(ENVELOPE_MAGIC)
    chunk_meta = cipher.decrypt_json(
        chunk["meta_enc"], aad=f"memory_chunks:meta_enc:{ids['chunk']}"
    )
    assert chunk_meta["filename"] == "invoice.pdf"
    assert chunk_meta["location"] == "page 2"


@pytest.mark.integration
async def test_seal_backfill_is_idempotent(db) -> None:
    cipher = _cipher()
    await _seed(db, cipher)
    await backfill(db, cipher, batch=50, sleep=0)
    counts = await pending_counts(db)
    assert all(count == 0 for count in counts.values())

    second = await backfill(db, cipher, batch=50, sleep=0)
    assert sum(result.sealed for result in second) == 0
    assert all(result.failed == 0 for result in second)


@pytest.mark.integration
async def test_sealer_fills_a_legacy_identity_and_handle(db) -> None:
    cipher = _cipher()
    identity_id = await db.fetchval(
        "INSERT INTO identities (display_name, confidence) VALUES ('Old Name', 0.5) RETURNING id"
    )
    handle_id = await db.fetchval(
        "INSERT INTO identity_handles (identity_id, platform, raw_handle, normalized)"
        " VALUES ($1, 'gmail', 'old@example.com', 'email:old@example.com') RETURNING id",
        identity_id,
    )
    results = await backfill(
        db, cipher, batch=10, sleep=0, tables={"identities", "identity_handles"}
    )
    assert [result.sealed for result in results] == [1, 1]
    profile = await db.fetchrow("SELECT profile_enc FROM identities WHERE id = $1", identity_id)
    assert cipher.decrypt_json(
        profile["profile_enc"], aad=f"identities:profile_enc:{identity_id}"
    ) == {"display_name": "Old Name", "confidence": 0.5}
    handle = await db.fetchrow("SELECT handle_enc FROM identity_handles WHERE id = $1", handle_id)
    assert cipher.decrypt_json(
        handle["handle_enc"], aad=f"identity_handles:handle_enc:{handle_id}"
    ) == {"platform": "gmail", "handle": "old@example.com"}


@pytest.mark.integration
async def test_unreadable_payload_is_left_alone(db) -> None:
    cipher = _cipher()
    await db.execute(
        "INSERT INTO events (source, kind, occurred_at, content_hash, payload_enc)"
        " VALUES ('gmail', 'message', now(), 'broken-hash', $1)",
        b"not a sealed payload",
    )
    results = await backfill(db, cipher, batch=50, sleep=0, tables={"events"})
    assert results[0].failed == 1
    assert results[0].remaining == 1
    row = await db.fetchrow("SELECT meta_enc FROM events WHERE content_hash = 'broken-hash'")
    assert row["meta_enc"] is None


@pytest.mark.integration
async def test_migrations_are_rerunnable(db) -> None:
    assert await run_migrations(db) == []
