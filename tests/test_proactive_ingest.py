"""The proactive path on the real ingest seam.

One urgent mail through `EventStore.ingest` must become exactly one alert,
with no restart repeating it, and a reminder ledger must survive a fresh
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


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, text: str, *, urgent: float = 0.0, channel: str = "") -> dict:
        self.sent.append({"text": text, "urgent": urgent, "channel": channel})
        return {"web": "m1"}


def _wire(db, *, notify: Recorder | None = None, clock=None):
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
        notify=notify,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    events.attach_proactive(path)
    return events, watches, commitments, path


@pytest.mark.integration
async def test_an_urgent_mail_on_the_ingest_hook_alerts_once(db) -> None:
    recorder = Recorder()
    events, watches, _, path = _wire(db, notify=recorder)
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

    assert len(recorder.sent) == 1
    assert "vedant@other.example" in recorder.sent[0]["text"]

    # a second evaluation of the same event — the restart case — is silent
    assert await path.evaluate_event(await events.get(result.event_id)) is None
    assert len(recorder.sent) == 1


@pytest.mark.integration
async def test_a_reminder_ledger_survives_a_new_path_object(db) -> None:
    commitments = Commitments(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))
    stored = await commitments.add(
        kind="task", text="send the invoice", due_at=NOW + timedelta(minutes=10), urgency=2
    )
    assert stored is not None

    recorder = Recorder()
    first = ProactivePath(
        config=AppConfig(proactive=ProactiveConfig()),
        commitments=commitments,
        reminders=CommitmentReminders(db),
        notify=recorder,
        timezone="UTC",
        clock=lambda: NOW,
    )
    await first.refresh()
    assert await first.tick() == 1

    # a fresh object after a restart reads the same ledger and stays silent
    second = ProactivePath(
        config=AppConfig(proactive=ProactiveConfig()),
        commitments=commitments,
        reminders=CommitmentReminders(db),
        notify=recorder,
        timezone="UTC",
        clock=lambda: NOW,
    )
    await second.refresh()
    assert await second.tick() == 0
    assert len(recorder.sent) == 1
