"""The proactive path: watch matches become observation context, reminders
on schedule.

The path is exercised over small in-memory doubles so the decisions are
visible: which event matched which watch, when a reminder fired, how many
times. The store and cipher are covered by test_watches.py and the
integration suite.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from aether.config import AppConfig, ProactiveConfig
from aether.memory.commitments import OPEN, Commitment
from aether.memory.events import Event
from aether.memory.watches import ACTIVE, Watch, WatchSpec
from aether.proactive.path import ProactivePath

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


class FakeWatches:
    """WatchStore double with the same dedup and watermark shape."""

    def __init__(self) -> None:
        self.rows: list[Watch] = []
        self.runs: list[tuple[list[int], int]] = []
        self._next = 1

    async def create(self, *, spec: WatchSpec, origin: str = "owner", actor: str = "owner"):
        if origin != "owner":
            return None
        watch = Watch(
            id=self._next,
            spec=spec,
            status=ACTIVE,
            last_event_id=0,
            last_run_at=None,
            last_notified_at=None,
            created_at=NOW,
        )
        self._next += 1
        self.rows.append(watch)
        return watch

    async def list_active(self) -> list[Watch]:
        return [watch for watch in self.rows if watch.status == ACTIVE]

    async def list(self, limit: int = 50) -> list[Watch]:
        return list(reversed(self.rows))[:limit]

    async def set_enabled(self, watch_id: int, enabled: bool, *, origin: str = "owner"):
        if origin != "owner":
            return None
        for watch in self.rows:
            if watch.id == watch_id:
                watch.status = ACTIVE if enabled else "paused"
                return watch
        return None

    async def delete(self, watch_id: int, *, origin: str = "owner") -> bool:
        if origin != "owner":
            return False
        before = len(self.rows)
        self.rows = [watch for watch in self.rows if watch.id != watch_id]
        return len(self.rows) != before

    async def mark_run(self, watch_ids: list[int], last_event_id: int) -> None:
        self.runs.append((list(watch_ids), last_event_id))
        for watch in self.rows:
            if watch.id in watch_ids:
                watch.last_event_id = max(watch.last_event_id, last_event_id)


class FakeCommitments:
    def __init__(self, items: list[Commitment]) -> None:
        self.items = items
        self.reads = 0

    async def list_open(self, limit: int = 50) -> list[Commitment]:
        self.reads += 1
        return list(self.items)


def _mail(
    event_id: int = 1,
    *,
    urgency: int = 3,
    sender: str = "Rahul <rahul@example.com>",
    summary: str = "production is down",
    noise: bool = False,
    occurred_at: datetime | None = None,
) -> Event:
    return Event(
        id=event_id,
        source="gmail",
        kind="mail",
        occurred_at=occurred_at or NOW,
        payload={"sender": sender, "subject": "Urgent"},
        salience_score=0.0,
        memorable=True,
        meta={},
        urgency=urgency,
        noise=noise,
        summary=summary,
    )


def _config(**overrides) -> AppConfig:
    defaults = {"watches_enabled": True}
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
    watches: FakeWatches | None = None,
    commitments: FakeCommitments | None = None,
    config: AppConfig | None = None,
    clock=None,
    channels=None,
) -> ProactivePath:
    path = ProactivePath(
        config=config or _config(),
        watches=watches,
        commitments=commitments,
        channels=channels,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    await path.refresh()
    return path


# ---------------------------------------------------------------------------
# watch matches
# ---------------------------------------------------------------------------


async def test_an_urgent_mail_is_an_observation_not_a_card() -> None:
    path = await _path()

    assert await path.evaluate_event(_mail()) == []


async def test_a_watch_match_returns_its_label_without_sending() -> None:
    watches = FakeWatches()
    await watches.create(
        spec=WatchSpec(label="urgent mail", scope="mail", urgency_min=2, channel="telegram"),
        origin="owner",
    )
    path = await _path(watches=watches)

    labels = await path.evaluate_event(_mail(urgency=2, sender="Vedant <vedant@other.example>"))

    assert labels == ["urgent mail"]
    # the watermark moved, so the same event is not matched a second time
    assert watches.rows[0].last_event_id == 1
    assert await path.evaluate_event(_mail(urgency=2)) == []


async def test_a_second_matching_mail_returns_its_label_again() -> None:
    watches = FakeWatches()
    await watches.create(spec=WatchSpec(label="any mail", scope="mail"), origin="owner")
    path = await _path(watches=watches)

    assert await path.evaluate_event(_mail(1)) == ["any mail"]
    assert await path.evaluate_event(_mail(2)) == ["any mail"]


async def test_a_non_urgent_mail_matches_nothing_and_stays_quiet() -> None:
    path = await _path()

    assert await path.evaluate_event(_mail(urgency=1)) == []


async def test_a_noise_event_never_matches_even_with_a_watch() -> None:
    watches = FakeWatches()
    await watches.create(spec=WatchSpec(label="x", scope="mail"), origin="owner")
    path = await _path(watches=watches)

    assert await path.evaluate_event(_mail(urgency=3, noise=True)) == []


async def test_a_watch_on_a_disconnected_surface_stores_every_surface() -> None:
    watches = FakeWatches()
    path = await _path(watches=watches, channels=lambda: {"web"})

    watch = await path.create_watch(
        WatchSpec(label="mail", scope="mail", channel="telegram"), origin="owner"
    )

    assert watch is not None
    assert watch.channel == ""


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
