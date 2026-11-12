"""Commitment reminders: urgency-scaled leads, one overdue escalation.

`reminder_due` is the whole policy as a pure function, so these pin the
ladder without a clock, a database or a model. The integration test proves
the ledger survives a restart by refusing to repeat a lead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.memory.commitments import OPEN, Commitment
from aether.proactive.path import (
    OVERDUE,
    CommitmentReminders,
    leads_for,
    reminder_due,
    reminder_payload,
)

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _commitment(
    *,
    due_at: datetime | None,
    urgency: int = 0,
    status: str = OPEN,
    text: str = "send the invoice",
) -> Commitment:
    return Commitment(
        id=1,
        kind="task",
        status=status,
        text=text,
        due_at=due_at,
        counterparty_identity_id=None,
        source_event_id=None,
        created_at=NOW,
        urgency=urgency,
    )


def test_leads_scale_with_urgency() -> None:
    assert leads_for(3) == (15, 120, 1440)
    assert leads_for(2) == (15, 120)
    assert leads_for(1) == (15,)
    assert leads_for(0) == (15,)


def test_an_undated_or_closed_commitment_is_never_reminded() -> None:
    assert reminder_due(_commitment(due_at=None), NOW) is None
    assert reminder_due(_commitment(due_at=NOW + timedelta(minutes=5), status="done"), NOW) is None


def test_a_far_due_date_waits_for_its_first_milestone() -> None:
    assert reminder_due(_commitment(due_at=NOW + timedelta(days=3), urgency=3), NOW) is None


def test_the_nearest_milestone_wins_after_downtime() -> None:
    # 90 minutes before due: the 24h and 2h milestones have passed, but the
    # 15-minute one has not — the most recent missed milestone fires once
    assert reminder_due(_commitment(due_at=NOW + timedelta(minutes=90), urgency=3), NOW) == 120


def test_the_fifteen_minute_nudge_fires_last() -> None:
    assert reminder_due(_commitment(due_at=NOW + timedelta(minutes=10), urgency=2), NOW) == 15


def test_a_sent_lead_does_not_fire_again() -> None:
    due = NOW + timedelta(minutes=90)
    assert reminder_due(_commitment(due_at=due, urgency=3), NOW, {120}) is None


def test_a_missed_older_lead_does_not_fire_retroactively() -> None:
    # ten minutes before due, the 15-minute milestone is current; the 24h
    # and 2h leads that came and went during downtime are superseded
    due = NOW + timedelta(minutes=10)
    assert reminder_due(_commitment(due_at=due, urgency=3), NOW) == 15
    assert reminder_due(_commitment(due_at=due, urgency=3), NOW, {15}) is None


def test_each_milestone_fires_as_time_passes() -> None:
    due = NOW + timedelta(minutes=100)
    assert reminder_due(_commitment(due_at=due, urgency=2), NOW) == 120
    assert (
        reminder_due(_commitment(due_at=due, urgency=2), NOW + timedelta(minutes=60), {120}) is None
    )
    late = reminder_due(_commitment(due_at=due, urgency=2), NOW + timedelta(minutes=90), {120})
    assert late == 15


def test_overdue_fires_exactly_once() -> None:
    overdue = _commitment(due_at=NOW - timedelta(hours=1), urgency=3)
    assert reminder_due(overdue, NOW) == OVERDUE
    assert reminder_due(overdue, NOW, {OVERDUE}) is None


def test_a_reminder_is_facts_for_the_agent_not_a_sentence() -> None:
    payload = reminder_payload(
        _commitment(due_at=NOW + timedelta(minutes=10), text="file the taxes"),
        lead=15,
        timezone="Asia/Kolkata",
    )

    assert payload["text"] == "file the taxes"
    assert payload["due_local"] == "2026-11-03 11:56 IST"
    assert payload["lead_minutes"] == 15
    assert payload["overdue"] is False
    assert set(payload) == {"commitment_id", "text", "due_local", "overdue", "lead_minutes"}


def test_an_overdue_reminder_is_marked_overdue() -> None:
    payload = reminder_payload(
        _commitment(due_at=NOW - timedelta(hours=1)), lead=OVERDUE, timezone="UTC"
    )

    assert payload["overdue"] is True
    assert payload["lead_minutes"] == OVERDUE


@pytest.mark.integration
async def test_the_reminder_ledger_survives_a_restart(db) -> None:
    from aether.authz.audit import AuditLog
    from aether.memory.commitments import Commitments
    from aether.memory.crypto import Cipher, generate_key_b64

    commitments = Commitments(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))
    stored = await commitments.add(kind="task", text="send the invoice")
    assert stored is not None
    reminders = CommitmentReminders(db)
    await reminders.mark(stored.id, 120)
    await reminders.mark(stored.id, 120)  # the unique index makes this a no-op
    await reminders.mark(stored.id, OVERDUE)

    assert await reminders.load() == {stored.id: {120, OVERDUE}}
