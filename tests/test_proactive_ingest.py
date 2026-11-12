"""The proactive path on the real ingest seam.

A matching mail through `EventStore.ingest` must be tagged once with its
watch label and send nothing, and a reminder ledger must survive a fresh
path object. These run against a real database because the sealed spec,
the watermark and the reminder ledger are what make the guarantees hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.authz.audit import AuditLog
from aether.config import AppConfig, ContactsConfig, ProactiveConfig
from aether.memory.commitments import Commitments
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import EventStore
from aether.memory.watches import WatchSpec, WatchStore
from aether.proactive.path import CommitmentReminders, ProactivePath

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _wire(db, *, clock=None):
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    events = EventStore(db, cipher, audit, ContactsConfig())
    watches = WatchStore(db, cipher, audit)
    commitments = Commitments(db, cipher, audit)
    reminders = CommitmentReminders(db)
    path = ProactivePath(
        config=AppConfig(proactive=ProactiveConfig()),
        watches=watches,
        commitments=commitments,
        reminders=reminders,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    events.attach_proactive(path)
    return events, watches, commitments, path


@pytest.mark.integration
async def test_a_watch_match_tags_the_event_without_sending(db) -> None:
    events, watches, _, path = _wire(db)
    await watches.create(
        spec=WatchSpec(label="urgent mail", scope="mail", urgency_min=2), origin="owner"
    )
    await path.refresh()

    result = await events.ingest(
        source="gmail",
        kind="mail",
        payload={
            "subject": "Server is down",
            "sender": "Vedant <vedant@other.example>",
            "snippet": "urgent, please fix now",
        },
        occurred_at=NOW,
    )
    assert result.stored
    await events.wait_idle()

    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.meta.get("watch_labels") == ["urgent mail"]

    # a second evaluation of the same event — the restart case — is silent
    assert await path.evaluate_event(stored) == []


@pytest.mark.integration
async def test_a_reminder_ledger_survives_a_new_path_object(db) -> None:
    commitments = Commitments(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))
    stored = await commitments.add(
        kind="task", text="send the invoice", due_at=NOW + timedelta(minutes=10), urgency=2
    )
    assert stored is not None

    first = ProactivePath(
        config=AppConfig(proactive=ProactiveConfig()),
        commitments=commitments,
        reminders=CommitmentReminders(db),
        timezone="UTC",
        clock=lambda: NOW,
    )
    await first.refresh()
    due = await first.tick()
    assert len(due) == 1
    assert due[0].payload["overdue"] is False

    # a fresh object after a restart reads the same ledger and stays silent
    second = ProactivePath(
        config=AppConfig(proactive=ProactiveConfig()),
        commitments=commitments,
        reminders=CommitmentReminders(db),
        timezone="UTC",
        clock=lambda: NOW,
    )
    await second.refresh()
    assert await second.tick() == []
