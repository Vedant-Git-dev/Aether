"""Commitment reminders: the clock-driven leads, once each.

The path is exercised over small in-memory doubles so the decisions are
visible: when a reminder fired, which lead, how many times. Alert matching
lives in test_alerts.py — the two engines are deliberately apart.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from aether.config import AppConfig, ProactiveConfig
from aether.memory.commitments import OPEN, Commitment
from aether.proactive.path import ProactivePath

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


class FakeCommitments:
    def __init__(self, items: list[Commitment]) -> None:
        self.items = items
        self.reads = 0

    async def list_open(self, limit: int = 50) -> list[Commitment]:
        self.reads += 1
        return list(self.items)


def _config(**overrides) -> AppConfig:
    defaults = {}
    defaults.update(overrides)
    return AppConfig(proactive=ProactiveConfig(**defaults))


def _commitment(
    due_at: datetime, *, urgency: int = 2, text: str = "send the invoice"
) -> Commitment:
    return Commitment(
        id=1,
        kind="task",
        status=OPEN,
        text=text,
        due_at=due_at,
        counterparty_identity_id=None,
        source_event_id=None,
        created_at=NOW,
        urgency=urgency,
    )


async def _path(
    *,
    commitments: FakeCommitments | None = None,
    config: AppConfig | None = None,
    clock=None,
) -> ProactivePath:
    path = ProactivePath(
        config=config or _config(),
        commitments=commitments,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    await path.refresh()
    return path


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


async def test_a_reminder_fires_once_then_the_overdue_escalation_fires_once() -> None:
    item = _commitment(NOW + timedelta(minutes=10))
    commitments = FakeCommitments([item])
    clock = [NOW]
    path = await _path(commitments=commitments, clock=lambda: clock[0])

    first = await path.tick()
    assert len(first) == 1
    assert first[0].payload["text"] == "send the invoice"
    assert first[0].payload["overdue"] is False
    assert await path.tick() == []  # the same lead never repeats

    commitments.items[0] = replace(item, due_at=NOW - timedelta(minutes=1))
    clock[0] = NOW + timedelta(hours=1)
    overdue = await path.tick()
    assert len(overdue) == 1
    assert overdue[0].payload["overdue"] is True
    assert await path.tick() == []


async def test_reminders_can_be_switched_off() -> None:
    commitments = FakeCommitments([_commitment(NOW + timedelta(minutes=5))])
    path = await _path(commitments=commitments, config=_config(reminders_enabled=False))

    assert await path.tick() == []
