"""The batch queue's monthly meter — one row per month, whole minutes.

The batch provider bills wall-clock time, so its meter counts seconds
under the same trade the daily counters make: plain columns, counts
only, never any content — which month ran long, not what ran. A month
with no batch work has no row, and twelve rows a year is the whole
table, so there is nothing to prune.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# the batch queue bills per started minute, so every charge is rounded
# up to whole minutes the same way the bill will be — a 90-second job
# is 2 minutes here and 2 minutes on the invoice, never a surprise gap


def month(now: datetime | None = None) -> str:
    """The month key for a moment — YYYY-MM in the caller's local time."""
    return (now or datetime.now()).strftime("%Y-%m")


@dataclass(frozen=True)
class MeterState:
    used_minutes: int
    cap_minutes: int
    allowed: bool
    near_limit: bool  # at or past 80% of the cap — the warning line


class MinuteMeter:
    """Batch minutes in Postgres — one numeric upsert per charge."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def charge(self, seconds: float, *, now: datetime | None = None) -> int:
        """Add a job's wall-clock seconds, rounded up to whole minutes,
        and return the month's new total. First charge of a month
        creates its row; the insert and the increment are one statement,
        so concurrent charges cannot lose a minute."""
        minutes = max(1, math.ceil(seconds / 60))
        key = month(now)
        row = await self._pool.fetchrow(
            "INSERT INTO workbench_minutes (month, seconds)"
            " VALUES ($1, $2)"
            " ON CONFLICT (month) DO UPDATE SET"
            " seconds = workbench_minutes.seconds + EXCLUDED.seconds,"
            " updated_at = now()"
            " RETURNING seconds",
            key,
            minutes * 60,
        )
        return int(row["seconds"]) // 60

    async def check(self, cap_minutes: int, *, now: datetime | None = None) -> MeterState:
        """This month against the cap: whether another job may run, and
        whether the total has crossed the warning line (80%). A meter
        outage is the caller's problem — this method only ever reads."""
        row = await self._pool.fetchrow(
            "SELECT seconds FROM workbench_minutes WHERE month = $1", month(now)
        )
        used_minutes = (int(row["seconds"]) if row else 0) // 60
        return MeterState(
            used_minutes=used_minutes,
            cap_minutes=cap_minutes,
            allowed=used_minutes < cap_minutes,
            near_limit=used_minutes * 5 >= cap_minutes * 4,
        )
