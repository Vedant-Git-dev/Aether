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
from aether.authz.approvals import Approvals
from aether.authz.audit import AuditLog
from aether.chat.ws import ChatHistory
from aether.config import ContactsConfig, MediaConfig
from aether.media.store import MediaStore
from aether.memory.commitments import Commitments
from aether.memory.conclusions import Conclusions
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.deliveries import Deliveries
from aether.memory.entities import Entities, Sender
from aether.memory.events import EventStore
from aether.memory.focus import Focus
from aether.memory.watches import WatchSpec, WatchStore
from aether.proactive.store import ProactiveStore
from aether.providers.crypto import TokenKeyring
from aether.providers.store import ProviderStore
from aether.routines.store import Routines
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

        routines = Routines(db, cipher, audit)
        await routines.create(
            label=f"label {canary}",
            trigger={"subject_contains": canary},
            action={"tool": "gmail__send", "params": {"body": canary}},
        )

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
        watches = WatchStore(db, cipher, audit)
        await watches.create(
            spec=WatchSpec(
                label=f"label {canary}",
                scope="mail",
                sender=f"{canary}@example.com",
                contains=canary,
            ),
            origin="owner",
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
