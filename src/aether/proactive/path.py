"""Commitment reminders: the time-based leads a due commitment earns.

A commitment Aether noticed (a promise in mail, a deadline in a message) can
carry a due time. This path reports the lead that has arrived — nearest
first, once each, then one overdue escalation — as facts, never as a
sentence. The loop feeds them to the agent, which writes the words.

Event alerts are a different engine (`aether.alerts`): they match sealed
specs against inbound events and deliver through their own deterministic
queue. Keeping the two apart is deliberate — a reminder is a clock
comparison, an alert is a pattern match, and neither needs the other's
state.

The model is nowhere in either decision. A reminder is a comparison against
an absolute due time; an alert match is pure code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..config import AppConfig
from ..memory.commitments import OPEN, Commitment, Commitments
from ..memory.timewindow import local_stamp

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
    """Commitment reminders — the clock-driven half of the proactive path."""

    def __init__(
        self,
        *,
        config: AppConfig,
        commitments: Commitments | None = None,
        reminders: CommitmentReminders | None = None,
        timezone: str = "Asia/Kolkata",
        clock: Any = None,
    ) -> None:
        self._config = config  # THE live object; every gate is read per call
        self._commitments = commitments
        self._reminders = reminders
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._reminded: dict[int, set[int]] = {}

    # -- lifecycle -----------------------------------------------------------

    async def refresh(self) -> None:
        """Reload the reminder ledger. Called at boot and after every owner
        change, so a restart cannot re-fire an old reminder."""
        async with self._load_lock:
            if self._reminders is not None:
                self._reminded = await self._reminders.load()
            self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.refresh()

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
