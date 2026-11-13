"""The alert engine on the real ingest seam.

A matching mail through `EventStore.ingest` must queue exactly one sealed
candidate and send nothing by itself; the engine's own tick is what delivers,
with no model turn in the path. A reminder ledger must survive a fresh path
object. These run against a real database because the sealed spec, the
watermark and the notification queue are what make the guarantees hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.alerts.engine import AlertEngine
from aether.alerts.notifications import SENT, AlertNotifications
from aether.alerts.spec import AlertSpec
from aether.alerts.store import AlertStore
from aether.authz.audit import AuditLog
from aether.config import AppConfig, ContactsConfig, ProactiveConfig
from aether.memory.commitments import Commitments
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import EventStore
from aether.origin import owner_message
from aether.proactive.path import CommitmentReminders, ProactivePath

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _wire(db, *, clock=None, sent=None):
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    events = EventStore(db, cipher, audit, ContactsConfig())
    alerts = AlertStore(db, cipher, audit)
    notifications = AlertNotifications(db, cipher)
    engine = AlertEngine(
        config=AppConfig(proactive=ProactiveConfig()),
        store=alerts,
        notifications=notifications,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    sent = sent if sent is not None else []

    async def notify(text: str, *, urgent: float = 0.0, channel: str = ""):
        sent.append(text)
        return {"web": "1"}

    engine.attach_notify(notify)
    events.attach_proactive(engine)
    return events, alerts, notifications, engine, sent


@pytest.mark.integration
async def test_a_match_queues_one_candidate_and_sends_nothing_by_itself(db) -> None:
    events, alerts, notifications, engine, sent = _wire(db)
    await alerts.create(spec=AlertSpec(title="urgent mail", scope="mail"), origin=owner_message())
    await engine.refresh()

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
    assert stored.meta.get("alert_matches") == ["urgent mail"]
    assert sent == []

    pending = await notifications.recent()
    assert len(pending) == 1
    assert pending[0].status != SENT

    # exactly one delivery, through the engine's own pass
    assert await engine.tick(NOW) == 1
    assert len(sent) == 1
    assert (await notifications.recent())[0].status == SENT

    # the same event is never queued again, even after a restart
    fresh = AlertEngine(
        config=AppConfig(proactive=ProactiveConfig()),
        store=alerts,
        notifications=notifications,
        timezone="UTC",
        clock=lambda: NOW,
    )
    await fresh.refresh()
    assert await fresh.evaluate_event(stored) == []
    assert await fresh.tick(NOW) == 0


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
