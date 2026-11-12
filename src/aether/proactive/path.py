"""The proactive path: watch matching on arrivals, and commitment reminders.

Two deterministic jobs share one object so the loop has one seam to call:

- ``evaluate_event`` runs once per ingested event (from the event store's
  tracked enrichment task). Each active watch keeps an id watermark, so an
  event is considered once per watch even across a restart. A match writes
  the watch label onto the event as context; it sends nothing. The agent
  reads that label with its ordinary observations and writes any message
  itself, under the same notification policy as every other unprompted turn.

- ``tick`` runs on the loop's cadence and reports commitments whose reminder
  lead has arrived. Leads scale with the source event's urgency, and each
  lead fires once; after the due time there is one overdue escalation and
  no more. A due reminder is returned as facts, never as a sentence; the
  loop feeds it to the agent, which writes the words itself.

The model is nowhere in either decision. A watch is code matching a sealed
spec, and a reminder is a comparison against an absolute due time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..config import AppConfig
from ..memory.commitments import OPEN, Commitment, Commitments
from ..memory.timewindow import local_stamp
from ..memory.watches import Watch, WatchSpec, WatchStore, watch_matches

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


@dataclass(frozen=True)
class DueReminder:
    """One commitment whose reminder lead arrived, as facts for the agent.

    ``payload`` carries the commitment, its due time in the user's zone,
    and whether the lead is the overdue escalation. Nothing here is a
    sentence: the loop hands this to the agent, which writes the words.
    """

    payload: dict[str, Any]
    urgency: int
    salience: float


def reminder_payload(
    commitment: Commitment, lead: int, *, timezone: str = "Asia/Kolkata"
) -> dict[str, Any]:
    """The facts of one due reminder — no wording, no instructions."""
    due = commitment.due_at
    return {
        "commitment_id": commitment.id,
        "text": commitment.text,
        "due_local": local_stamp(due, timezone) if due is not None else "",
        "overdue": lead == OVERDUE,
        "lead_minutes": lead,
    }


def _alert_salience(urgency: int) -> float:
    """Urgency on the 0-10 scale the interruption budget already reads, so
    a level-2 reminder clears the default quiet-hours threshold of 8."""
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


class ProactivePath:
    """Watch matching and commitment reminders."""

    def __init__(
        self,
        *,
        config: AppConfig,
        watches: WatchStore | None = None,
        commitments: Commitments | None = None,
        reminders: CommitmentReminders | None = None,
        channels: Callable[[], set[str]] | None = None,
        timezone: str = "Asia/Kolkata",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config  # THE live object; every gate is read per call
        self._watches = watches
        self._commitments = commitments
        self._reminders = reminders
        # the surfaces a watch may name right now — read live, because a
        # connector can be switched on or off without a restart
        self._channels = channels
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active: list[Watch] = []
        self._loaded = False
        self._load_lock = asyncio.Lock()
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
        """Arm a watch through the owner gate and make it live immediately.

        A channel the user named but that is not connected right now is
        dropped to "every surface" rather than stored, so the saved spec
        never points at a connector that is switched off. The confirmation
        reads the normalized spec back.
        """
        if self._watches is None:
            return None
        spec = replace(spec, channel=self._normalized_channel(spec.channel))
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

    async def update_watch(self, watch_id: int, spec: WatchSpec, *, origin: str) -> Watch | None:
        """Replace one alert's condition through the owner gate."""
        if self._watches is None:
            return None
        spec = replace(spec, channel=self._normalized_channel(spec.channel))
        watch = await self._watches.update(watch_id, spec=spec, origin=origin)
        if watch is not None:
            await self.refresh()
        return watch

    async def get_watch(self, watch_id: int) -> Watch | None:
        if self._watches is None:
            return None
        return await self._watches.get(watch_id)

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

    async def evaluate_event(self, event: Any) -> list[str]:
        """Match one ingested event against the active watches.

        Returns the labels of the watches it matched, so the event store can
        record them on the event as context. Nothing is sent from here: the
        agent sees the match in its ordinary observations and writes any
        message itself, under the same notification policy as every other
        unprompted turn. The watermark still moves for every event, so an
        event is considered once per watch even across a restart.
        """
        if not self._config.proactive.watches_enabled:
            return []
        if getattr(event, "noise", False):
            return []
        await self._ensure_loaded()
        matched = [
            watch
            for watch in self._active
            if event.id > watch.last_event_id and watch_matches(watch.spec, event)
        ]
        await self._advance(event.id)
        return [watch.spec.label for watch in matched if watch.spec.label]

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

    async def tick(self, now: datetime | None = None) -> list[DueReminder]:
        """Report commitments whose reminder lead has arrived. Each lead is
        recorded here so a restart never repeats it; the entries are facts
        for the agent, never ready-made words."""
        now = now or self._clock()
        await self._ensure_loaded()
        if not self._config.proactive.reminders_enabled or self._commitments is None:
            return []
        try:
            open_items = await self._commitments.list_open(limit=200)
        except Exception:
            log.warning("could not read open commitments for reminders", exc_info=True)
            return []
        due: list[DueReminder] = []
        for commitment in open_items:
            sent = self._reminded.get(commitment.id, set())
            lead = reminder_due(commitment, now, sent)
            if lead is None:
                continue
            self._reminded.setdefault(commitment.id, set()).add(lead)
            if self._reminders is not None:
                try:
                    await self._reminders.mark(commitment.id, lead)
                except Exception:
                    log.warning("could not record reminder for commitment %s", commitment.id)
            due.append(
                DueReminder(
                    payload=reminder_payload(commitment, lead, timezone=self._timezone),
                    urgency=commitment.urgency,
                    salience=_alert_salience(commitment.urgency),
                )
            )
        return due

    def _available_channels(self) -> set[str] | None:
        """The surfaces a watch may name right now, or None when the caller
        wired no channel source (tests) — then nothing can be validated."""
        if self._channels is None:
            return None
        try:
            return set(self._channels())
        except Exception:
            log.warning("could not read the connected surfaces", exc_info=True)
            return None

    def _normalized_channel(self, channel: str) -> str:
        """A named surface that is not connected becomes "every surface",
        so the stored watch never points at a connector that is switched off."""
        clean = (channel or "").strip()
        if not clean:
            return ""
        available = self._available_channels()
        if available is None or clean in available:
            return clean
        log.info("watch channel %r is not connected — storing all surfaces", clean)
        return ""
