"""The canary scan: one unique string through every write path, then a sweep
of every column, and no plaintext copy anywhere.

The cutover dropped the legacy columns, so the sweep must come back empty:
a canary anywhere is a leak. The sealed payload must still decrypt to
contain it, and the log capture must never see it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakePersonJudge

from aether.agent.traces import Traces
from aether.alerts.migrate import routines_to_alerts
from aether.alerts.notifications import AlertNotifications
from aether.alerts.spec import AlertSpec
from aether.alerts.store import AlertStore
from aether.authz.approvals import Approvals
from aether.authz.audit import AuditLog
from aether.chat.ws import ChatHistory
from aether.config import ContactsConfig, MediaConfig
from aether.media.store import MediaStore
from aether.memory.commitments import Commitments
from aether.memory.conclusions import Conclusions
from aether.memory.control import MemoryControl
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.deliveries import Deliveries
from aether.memory.entities import Entities, Sender
from aether.memory.events import EventStore
from aether.memory.facts import Facts
from aether.memory.focus import Focus
from aether.memory.indexes import MemoryIndex
from aether.memory.lexical import LexicalIndex
from aether.memory.summaries import Summaries
from aether.origin import owner_message
from aether.proactive.store import ProactiveStore
from aether.providers.crypto import TokenKeyring
from aether.providers.store import ProviderStore
from aether.scheduler.jobs import Scheduler
from aether.workspace.store import Workspace

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


async def _sweep(db, needle: str) -> list[str]:
    """Every column of every table, checked for the canary bytes."""
    token = needle.encode()
    found: list[str] = []
    columns = await db.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' ORDER BY table_name, column_name"
    )
    for row in columns:
        table, column = str(row["table_name"]), str(row["column_name"])
        if not (_IDENT.match(table) and _IDENT.match(column)):
            continue
        values = await db.fetch(f'SELECT "{column}" AS v FROM "{table}"')
        for value_row in values:
            value = value_row["v"]
            if value is None:
                continue
            blob = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
            if token in blob:
                found.append(f"{table}.{column}")
                break
    return found


@pytest.mark.integration
async def test_no_canary_leaks_into_any_column(db, caplog) -> None:
    canary = "AETHER-CANARY-7f3a91"
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    contacts = ContactsConfig()

    with caplog.at_level("DEBUG"):
        events = EventStore(db, cipher, audit, contacts)
        event = await events.ingest(
            source="gmail",
            kind="message",
            payload={"text": f"body {canary}", "subject": f"subject {canary}"},
            sender=Sender("gmail", f"{canary}@example.com"),
        )
        assert event.event_id is not None
        await events.update_salience(event.event_id, 9.0, True, category=canary)

        entities = Entities(db, cipher, audit, FakePersonJudge([]))
        person = await entities.resolve("gmail", f"{canary}@example.com", f"{canary} Name")
        await entities.note(person.id, {"kind": "general", "note": canary})

        approvals = Approvals(db, cipher, audit)
        await approvals.create(
            tool_name="gmail__send",
            params={"to": f"{canary}@example.com", "body": canary},
            rules_matched="builtin:risky",
            note=f"note {canary}",
        )

        scheduler = Scheduler(db, cipher, audit)
        await scheduler.create(
            label=f"label {canary}",
            run_at=datetime.now(UTC) + timedelta(hours=1),
            payload={"type": "tool", "tool": "gmail__send", "params": {"body": canary}},
        )

        alerts = AlertStore(db, cipher, audit)

        # a legacy routine row, then the real migration: both the old sealed
        # columns and the alert it becomes are swept for the canary
        row = await db.fetchrow(
            "INSERT INTO routines_legacy (label_enc, trigger_enc, action_enc, key_version)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            b"",
            b"",
            b"",
            cipher.key_id,
        )
        routine_id = int(row["id"])
        await db.execute(
            "UPDATE routines_legacy SET label_enc = $1, trigger_enc = $2, action_enc = $3"
            " WHERE id = $4",
            cipher.encrypt_text(f"label {canary}", aad=f"routines:label_enc:{routine_id}"),
            cipher.encrypt_json({"contains": canary}, aad=f"routines:trigger_enc:{routine_id}"),
            cipher.encrypt_json(
                {"type": "tool", "tool": "gmail__send", "params": {"body": canary}},
                aad=f"routines:action_enc:{routine_id}",
            ),
            routine_id,
        )
        await routines_to_alerts(db, cipher, alerts)

        traces = Traces(db, cipher)
        await traces.create(kind="turn", label=f"label {canary}", payload={"reply": canary})

        proactive = ProactiveStore(db, cipher, audit)
        await proactive.claim(
            skill="followups",
            key=f"key-{canary}",
            label=f"label {canary}",
            opportunity={"title": canary},
            day="2026-11-03",
        )

        media = MediaStore(db, cipher, MediaConfig())
        await media.put(f"bytes {canary}".encode(), filename=f"{canary}.png", origin="telegram")

        chat = ChatHistory(db, cipher)
        await chat.append("telegram", "in", f"text {canary}")

        commitments = Commitments(db, cipher, audit)
        await commitments.add(
            kind="promise",
            text=f"send the deck with {canary}",
            due_phrase=f"by {canary}",
            urgency=2,
        )
        facts = Facts(db, cipher, audit)
        await facts.consolidate(
            subject_identity_id=None,
            subject=f"subject {canary}",
            key=f"key {canary}",
            value=f"value {canary}",
            provenance=[event.event_id],
        )
        summaries = Summaries(db, cipher)
        await summaries.upsert("daily", f"period {canary}", f"summary {canary}", [event.event_id])
        alert = await alerts.create(
            spec=AlertSpec(
                title=f"label {canary}",
                scope="mail",
                sender=f"{canary}@example.com",
                contains=canary,
            ),
            origin=owner_message({"message_ids": [canary]}),
        )
        assert alert is not None
        notifications = AlertNotifications(db, cipher)
        await notifications.enqueue(
            event=__import__("aether.alerts.spec", fromlist=["AlertEvent"]).AlertEvent(
                alert_id=alert.id,
                event_id=1,
                source="gmail",
                kind="mail",
                subject=f"subject {canary}",
            ),
            style="immediate",
        )
        await Entities(db, cipher, audit, FakePersonJudge([])).note(
            1, {"kind": "general", "note": f"note {canary}", "address": f"{canary}@x.com"}
        )

        deliveries = Deliveries(db, cipher, audit)
        delivery = await deliveries.record_queued("telegram", f"body {canary}")
        assert delivery is not None
        await deliveries.mark_failed(delivery.id, f"error {canary}")

        conclusions = Conclusions(db, cipher)
        await conclusions.set(f"key {canary}", f"value {canary}", source=f"src {canary}")

        focus = Focus(db, cipher)
        await focus.record_turn(
            f"chat {canary}",
            entities=[1],
            offer={"text": canary, "options": [], "at": datetime.now(UTC).isoformat()},
            attachment={"media_id": 1, "filename": f"{canary}.png", "event_id": 1},
        )

        keyring = TokenKeyring.from_values(active_key_b64=generate_key_b64(), active_id="k1")
        providers = ProviderStore(db, keyring, cipher)
        await providers.set_sync("gmail", "files", "pageToken", f"cursor {canary}")

        workspace = Workspace(db, cipher=cipher)
        await workspace.ensure_scaffold()
        await workspace.remember(f"a note with {canary} in it", "fact")

        # forgetting leaves no readable copy either: the row and its chunks
        # are gone, and the tombstone is a keyed token, not the words
        doomed = await events.ingest(
            source="gmail",
            kind="message",
            payload={"text": f"forget me {canary}"},
            sender=Sender("gmail", f"gone-{canary}@example.com"),
        )
        assert doomed.event_id is not None
        control = MemoryControl(
            pool=db,
            cipher=cipher,
            audit=audit,
            events=events,
            index=MemoryIndex(),
            lexical=LexicalIndex(),
            facts=facts,
            commitments=commitments,
            summaries=summaries,
        )
        assert (await control.forget_event(doomed.event_id)).found
        assert await db.fetchval("SELECT count(*) FROM forgotten_events") == 1

    leaked = await _sweep(db, canary)
    assert leaked == [], f"canary in a column: {leaked}"

    # the sealed copy really holds the content, under the cipher
    row = await db.fetchrow(
        "SELECT payload_enc, meta_enc FROM events WHERE id = $1", event.event_id
    )
    payload = cipher.decrypt_json(row["payload_enc"], aad=f"events:payload_enc:{event.event_id}")
    assert canary in payload["text"]
    meta = cipher.decrypt_json(row["meta_enc"], aad=f"events:meta_enc:{event.event_id}")
    assert canary in meta["extra"]["category"]
    assert canary in await workspace.read("memory")

    # and nothing in the captured log lines
    assert canary not in caplog.text
