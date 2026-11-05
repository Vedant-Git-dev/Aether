"""The proactive path: watches firing on arrivals, and commitment reminders.

Two deterministic jobs share one object so the loop has one seam to call:

- ``evaluate_event`` runs once per ingested event (from the event store's
  tracked enrichment task). Each active watch keeps an id watermark, so an
  event is considered once per watch even across a restart. A matching
  event sends one plain card; the default urgent-mail policy covers the
  same duty when no watch is armed. Every alert is rate-limited, honours
  the watch's quiet window, and never quotes raw JSON or a tool name.

- ``tick`` runs on the loop's cadence: it flushes alerts that were held by
  a quiet window and fires commitment reminders whose lead has arrived.
  Leads scale with the source event's urgency, and each lead fires once;
  after the due time there is one overdue escalation and no more.

The model is nowhere in either decision. A watch is code matching a sealed
spec, and a reminder is a comparison against an absolute due time.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..config import AppConfig
from ..memory.commitments import OPEN, Commitment, Commitments
from ..memory.mail import MAIL_KIND
from ..memory.timewindow import in_quiet_window, local_stamp, parse_quiet_hours
from ..memory.watches import Watch, WatchSpec, WatchStore, render_alert, watch_matches

log = logging.getLogger("aether.proactive")

# urgency 3 gets the full ladder, 2 keeps the two closer ones, everything
# lower still gets the fifteen-minute nudge. Values are minutes before due.
_LEADS_BY_URGENCY: dict[int, tuple[int, ...]] = {
    3: (15, 120, 1440),
    2: (15, 120),
    1: (15,),
    0: (15,),
}

OVERDUE = -1

# the hourly cap counts every alert the path sends, watches and the default
# urgent policy together
_RATE_WINDOW = timedelta(hours=1)

# a small bounded memory of the events already announced, so a second call
# for the same event in one boot never double-sends
_ANNOUNCED_CAP = 5_000


def leads_for(urgency: int) -> tuple[int, ...]:
    """The reminder leads for one urgency, nearest milestone first."""
    try:
        value = max(0, min(3, int(urgency)))
    except (TypeError, ValueError):
        value = 0
    return _LEADS_BY_URGENCY[value]


def reminder_due(
    commitment: Commitment,
    now: datetime,
    sent: Iterable[int] = (),
) -> int | None:
    """The one lead that is due now, or -1 for the overdue escalation.

    Leads are checked nearest-first, so a long outage sends the most recent
    missed milestone once instead of replaying the whole ladder. An
    undated commitment gets no reminder at all; a closed one is never
    reminded.
    """
    if commitment.status != OPEN or commitment.due_at is None:
        return None
    already = set(sent)
    due = commitment.due_at
    if now >= due:
        return OVERDUE if OVERDUE not in already else None
    # the current milestone is the nearest lead whose time has arrived;
    # an older one that was missed during downtime is superseded, never
    # fired retroactively a moment before the closer one
    reached = [
        lead for lead in leads_for(commitment.urgency) if now >= due - timedelta(minutes=lead)
    ]
    if not reached:
        return None
    current = reached[0]
    return current if current not in already else None


def render_reminder(commitment: Commitment, lead: int, *, timezone: str = "Asia/Kolkata") -> str:
    """One plain reminder line, with the due time absolute in the user's
    timezone and no internal vocabulary."""
    due = commitment.due_at
    if due is None:  # defensive: reminder_due never returns one for this
        return f"Reminder: {commitment.text}"
    stamp = local_stamp(due, timezone)
    if lead == OVERDUE:
        head = f"Overdue: {commitment.text} — it was due {stamp}."
    else:
        when = _lead_words(lead)
        head = f"Reminder: {commitment.text} — due {stamp}" + (f" (in {when})." if when else ".")
    return f"{head}\nYou can tell me it's done or move the deadline."


def _lead_words(lead: int) -> str:
    if lead >= 1440:
        return f"{lead // 1440} day" + ("s" if lead // 1440 != 1 else "")
    if lead >= 120:
        return f"{lead // 60} hours"
    return f"{lead} minutes"


def _alert_salience(urgency: int) -> float:
    """Urgency on the 0-10 scale the interruption budget already reads, so
    a level-2 alert clears the default quiet-hours threshold of 8."""
    return {0: 2.0, 1: 4.0, 2: 8.0, 3: 10.0}.get(max(0, min(3, int(urgency))), 2.0)


class CommitmentReminders:
    """The ledger of which lead fired for which commitment — ids and
    integer minutes only, so a restart never repeats a reminder."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self) -> dict[int, set[int]]:
        rows = await self._pool.fetch(
            "SELECT commitment_id, lead_minutes FROM commitment_reminders"
        )
        found: dict[int, set[int]] = {}
        for row in rows:
            found.setdefault(int(row["commitment_id"]), set()).add(int(row["lead_minutes"]))
        return found

    async def mark(self, commitment_id: int, lead_minutes: int) -> None:
        await self._pool.execute(
            "INSERT INTO commitment_reminders (commitment_id, lead_minutes)"
            " VALUES ($1, $2) ON CONFLICT DO NOTHING",
            commitment_id,
            lead_minutes,
        )


Notify = Callable[..., Awaitable[Any]]
Owners = Callable[[], Awaitable[set[str]]]


class ProactivePath:
    """Watches, the default urgent-mail policy and commitment reminders."""

    def __init__(
        self,
        *,
        config: AppConfig,
        watches: WatchStore | None = None,
        commitments: Commitments | None = None,
        reminders: CommitmentReminders | None = None,
        notify: Notify | None = None,
        owners: Owners | None = None,
        timezone: str = "Asia/Kolkata",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config  # THE live object; every gate is read per call
        self._watches = watches
        self._commitments = commitments
        self._reminders = reminders
        self._notify = notify
        self._owners = owners
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active: list[Watch] = []
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._recent: deque[datetime] = deque()
        self._held: list[tuple[str, str, str, float]] = []
        self._announced: set[int] = set()
        self._announced_order: deque[int] = deque()
        self._reminded: dict[int, set[int]] = {}

    # -- lifecycle -----------------------------------------------------------

    async def refresh(self) -> None:
        """Reload the active watches and the reminder ledger. Called at
        boot and after every owner-made change, so a watch created in chat
        is live at once and a restart cannot re-fire an old reminder."""
        async with self._load_lock:
            if self._watches is not None:
                self._active = await self._watches.list_active()
            if self._reminders is not None:
                self._reminded = await self._reminders.load()
            self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.refresh()

    # -- watches -------------------------------------------------------------

    async def create_watch(self, spec: WatchSpec, *, origin: str) -> Watch | None:
        """Arm a watch through the owner gate and make it live immediately."""
        if self._watches is None:
            return None
        watch = await self._watches.create(spec=spec, origin=origin)
        if watch is not None:
            await self.refresh()
        return watch

    async def set_watch_enabled(self, watch_id: int, enabled: bool, *, origin: str) -> Watch | None:
        if self._watches is None:
            return None
        watch = await self._watches.set_enabled(watch_id, enabled, origin=origin)
        if watch is not None:
            await self.refresh()
        return watch

    async def delete_watch(self, watch_id: int, *, origin: str) -> bool:
        if self._watches is None:
            return False
        removed = await self._watches.delete(watch_id, origin=origin)
        if removed:
            await self.refresh()
        return removed

    async def list_watches(self, limit: int = 50) -> list[Watch]:
        if self._watches is None:
            return []
        return await self._watches.list(limit)

    async def evaluate_event(self, event: Any) -> str | None:
        """Consider one ingested event. Returns the card text when one was
        sent (or held for quiet hours), otherwise None."""
        if not self._config.proactive.watches_enabled:
            return None
        if getattr(event, "noise", False):
            return None
        await self._ensure_loaded()
        if event.id in self._announced:
            return None
        now = self._clock()
        matched = [
            watch
            for watch in self._active
            if event.id > watch.last_event_id and watch_matches(watch.spec, event)
        ]
        default = (
            not matched
            and self._config.proactive.urgent_mail_alerts
            and event.kind == MAIL_KIND
            and int(getattr(event, "urgency", 0)) >= self._config.proactive.urgent_urgency_min
        )
        if not matched and not default:
            await self._advance(event.id)
            return None
        if not self._rate_room(now):
            log.info("alert rate cap reached — event %s was not announced", event.id)
            await self._advance(event.id)
            return None
        watch = matched[0] if matched else None
        owners = await self._owner_set()
        text = render_alert(
            event,
            watch=watch,
            owners=owners,
            timezone=self._timezone,
            now=now,
        )
        sent = await self._send(
            text,
            channel=watch.channel if watch is not None else "",
            urgency=int(getattr(event, "urgency", 0)),
            quiet_hours=watch.spec.quiet_hours if watch is not None else "",
            now=now,
        )
        if sent:
            self._remember(event.id)
            self._recent.append(now)
        await self._advance(event.id)
        if matched and self._watches is not None and sent:
            for item in matched:
                await self._watches.mark_notified(item.id, now)
            for item in matched:
                item.last_notified_at = now
        for item in matched:
            item.last_event_id = max(item.last_event_id, event.id)
        return text if sent else None

    async def _advance(self, event_id: int) -> None:
        """Move every watch's watermark past this event, once, in one
        statement — matching and non-matching alike, so the next boot does
        not reconsider it."""
        if self._watches is None:
            return
        stale = [watch for watch in self._active if watch.last_event_id < event_id]
        if not stale:
            return
        await self._watches.mark_run([watch.id for watch in stale], event_id)
        for watch in stale:
            watch.last_event_id = event_id

    # -- reminders -----------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> int:
        """Flush held alerts and fire due reminders. Returns how many
        reminders went out (held alerts are counted by their own path)."""
        now = now or self._clock()
        await self._ensure_loaded()
        await self._flush_held(now)
        if not self._config.proactive.reminders_enabled or self._commitments is None:
            return 0
        fired = 0
        try:
            open_items = await self._commitments.list_open(limit=200)
        except Exception:
            log.warning("could not read open commitments for reminders", exc_info=True)
            return 0
        for commitment in open_items:
            sent = self._reminded.get(commitment.id, set())
            lead = reminder_due(commitment, now, sent)
            if lead is None:
                continue
            text = render_reminder(commitment, lead, timezone=self._timezone)
            await self._send(
                text,
                channel="",
                urgency=commitment.urgency,
                quiet_hours="",
                now=now,
            )
            self._reminded.setdefault(commitment.id, set()).add(lead)
            if self._reminders is not None:
                try:
                    await self._reminders.mark(commitment.id, lead)
                except Exception:
                    log.warning("could not record reminder for commitment %s", commitment.id)
            fired += 1
        return fired

    # -- delivery ------------------------------------------------------------

    async def _send(
        self,
        text: str,
        *,
        channel: str,
        urgency: int,
        quiet_hours: str,
        now: datetime,
    ) -> bool:
        """Send one alert, or hold it for the watch's own quiet window.
        A level-3 alert is urgent enough to break that window; everything
        lower waits for the window to end and then ships whole."""
        if (
            quiet_hours
            and urgency < 3
            and in_quiet_window(parse_quiet_hours(quiet_hours), now.astimezone())
        ):
            self._held.append((quiet_hours, text, channel, _alert_salience(urgency)))
            return True
        if self._notify is None:
            return True
        try:
            await self._notify(text, urgent=_alert_salience(urgency), channel=channel)
        except Exception:
            log.warning("could not deliver a proactive alert", exc_info=True)
            return False
        return True

    async def _flush_held(self, now: datetime | None = None) -> None:
        if not self._held:
            return
        now = now or self._clock()
        local = now.astimezone()
        waiting: list[tuple[str, str, str, float]] = []
        for quiet_hours, text, channel, salience in self._held:
            if in_quiet_window(parse_quiet_hours(quiet_hours), local):
                waiting.append((quiet_hours, text, channel, salience))
                continue
            if self._notify is None:
                continue
            try:
                await self._notify(text, urgent=salience, channel=channel)
            except Exception:
                log.warning("could not deliver a held alert", exc_info=True)
        self._held = waiting

    def _rate_room(self, now: datetime) -> bool:
        while self._recent and now - self._recent[0] > _RATE_WINDOW:
            self._recent.popleft()
        return len(self._recent) < self._config.proactive.max_alerts_per_hour

    def _remember(self, event_id: int) -> None:
        if event_id in self._announced:
            return
        self._announced.add(event_id)
        self._announced_order.append(event_id)
        while len(self._announced_order) > _ANNOUNCED_CAP:
            self._announced.discard(self._announced_order.popleft())

    async def _owner_set(self) -> set[str]:
        if self._owners is None:
            return set()
        try:
            return await self._owners()
        except Exception:
            log.warning("could not read the owner addresses for an alert", exc_info=True)
            return set()
