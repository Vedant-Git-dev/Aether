"""The proactive path: one alert per matching event, reminders on schedule.

The path is exercised over small in-memory doubles so the decisions are
visible: which event alerted, on which channel, how many times. The store
and cipher are covered by test_watches.py and the integration suite.
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
        self.notified: list[tuple[int, datetime]] = []
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

    async def mark_notified(self, watch_id: int, when: datetime | None = None) -> None:
        self.notified.append((watch_id, when or NOW))


class FakeCommitments:
    def __init__(self, items: list[Commitment]) -> None:
        self.items = items
        self.reads = 0

    async def list_open(self, limit: int = 50) -> list[Commitment]:
        self.reads += 1
        return list(self.items)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, text: str, *, urgent: float = 0.0, channel: str = "") -> dict:
        self.sent.append({"text": text, "urgent": urgent, "channel": channel})
        return {"web": "m1"}


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
    defaults = {"watches_enabled": True, "urgent_mail_alerts": True, "max_alerts_per_hour": 6}
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
    notify: Recorder | None = None,
    config: AppConfig | None = None,
    clock=None,
    channels=None,
) -> ProactivePath:
    path = ProactivePath(
        config=config or _config(),
        watches=watches,
        commitments=commitments,
        notify=notify,
        channels=channels,
        timezone="UTC",
        clock=clock or (lambda: NOW),
    )
    await path.refresh()
    return path


# ---------------------------------------------------------------------------
# arrival alerts
# ---------------------------------------------------------------------------


async def test_an_urgent_mail_alerts_exactly_once() -> None:
    recorder = Recorder()
    path = await _path(notify=recorder)

    first = await path.evaluate_event(_mail())
    second = await path.evaluate_event(_mail())

    assert first is not None and "rahul@example.com" in first
    assert second is None
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["urgent"] == 10.0


async def test_a_watch_match_names_the_watch_and_its_channel() -> None:
    watches = FakeWatches()
    await watches.create(
        spec=WatchSpec(label="urgent mail", scope="mail", urgency_min=2, channel="telegram"),
        origin="owner",
    )
    recorder = Recorder()
    path = await _path(watches=watches, notify=recorder)

    text = await path.evaluate_event(_mail(urgency=2, sender="Vedant <vedant@other.example>"))

    assert text is not None
    assert 'matched your watch "urgent mail"' in text
    assert "vedant@other.example" in text
    assert recorder.sent[0]["channel"] == "telegram"


async def test_a_second_matching_mail_alerts_again() -> None:
    watches = FakeWatches()
    await watches.create(spec=WatchSpec(label="any mail", scope="mail"), origin="owner")
    recorder = Recorder()
    path = await _path(watches=watches, notify=recorder)

    await path.evaluate_event(_mail(1))
    await path.evaluate_event(_mail(2))

    assert len(recorder.sent) == 2
    assert watches.notified[0][0] == 1


async def test_a_non_urgent_mail_is_quiet() -> None:
    recorder = Recorder()
    path = await _path(notify=recorder)

    assert await path.evaluate_event(_mail(urgency=1)) is None
    assert recorder.sent == []


async def test_a_noise_event_never_alerts_even_with_a_watch() -> None:
    watches = FakeWatches()
    await watches.create(spec=WatchSpec(label="x", scope="mail"), origin="owner")
    recorder = Recorder()
    path = await _path(watches=watches, notify=recorder)

    assert await path.evaluate_event(_mail(urgency=3, noise=True)) is None
    assert recorder.sent == []


async def test_the_rate_cap_defers_further_alerts_until_there_is_room() -> None:
    recorder = Recorder()
    clock = [NOW]
    path = await _path(
        notify=recorder, config=_config(max_alerts_per_hour=1), clock=lambda: clock[0]
    )

    await path.evaluate_event(_mail(1))
    await path.evaluate_event(_mail(2))

    assert len(recorder.sent) == 1  # the second is held, not lost

    clock[0] = NOW + timedelta(hours=2)
    await path.tick()

    assert len(recorder.sent) == 2


async def test_a_channel_that_is_not_connected_falls_back_to_all_surfaces() -> None:
    watches = FakeWatches()
    await watches.create(
        spec=WatchSpec(label="mail", scope="mail", channel="telegram"), origin="owner"
    )
    recorder = Recorder()
    path = await _path(watches=watches, notify=recorder, channels=lambda: {"web"})

    await path.evaluate_event(_mail(urgency=3))

    assert len(recorder.sent) == 1
    assert recorder.sent[0]["channel"] == ""


async def test_a_connected_channel_is_kept() -> None:
    watches = FakeWatches()
    await watches.create(
        spec=WatchSpec(label="mail", scope="mail", channel="telegram"), origin="owner"
    )
    recorder = Recorder()
    path = await _path(watches=watches, notify=recorder, channels=lambda: {"web", "telegram"})

    await path.evaluate_event(_mail(urgency=3))

    assert recorder.sent[0]["channel"] == "telegram"


async def test_a_watch_quiet_window_holds_then_flushes() -> None:
    watches = FakeWatches()
    await watches.create(
        spec=WatchSpec(label="quiet", scope="mail", quiet_hours="22:00-07:00"), origin="owner"
    )
    recorder = Recorder()
    clock = [datetime(2026, 11, 3, 23, 0, tzinfo=UTC).astimezone()]
    path = await _path(watches=watches, notify=recorder, clock=lambda: clock[0])

    await path.evaluate_event(_mail(urgency=2))
    assert recorder.sent == []  # held for the window

    clock[0] = datetime(2026, 11, 4, 8, 0, tzinfo=UTC).astimezone()
    await path.tick()

    assert len(recorder.sent) == 1


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


async def test_a_reminder_fires_once_then_the_overdue_escalation_fires_once() -> None:
    item = _commitment(NOW + timedelta(minutes=10))
    commitments = FakeCommitments([item])
    recorder = Recorder()
    clock = [NOW]
    path = await _path(commitments=commitments, notify=recorder, clock=lambda: clock[0])

    assert await path.tick() == 1
    assert await path.tick() == 0  # the same lead never repeats
    assert len(recorder.sent) == 1

    commitments.items[0] = replace(item, due_at=NOW - timedelta(minutes=1))
    clock[0] = NOW + timedelta(hours=1)
    assert await path.tick() == 1
    assert "Overdue" in recorder.sent[1]["text"]
    assert await path.tick() == 0


async def test_reminders_can_be_switched_off() -> None:
    commitments = FakeCommitments([_commitment(NOW + timedelta(minutes=5))])
    recorder = Recorder()
    path = await _path(
        commitments=commitments, notify=recorder, config=_config(reminders_enabled=False)
    )

    assert await path.tick() == 0
    assert recorder.sent == []
