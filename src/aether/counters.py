"""Daily counters — one row per day, counts only, never any content.

The shape the web search cap and the proactive budgets share: bump a
count, read a count, both against today's row. Nothing personal is stored
here — the same trade the scheduled_actions fire counts already make — so
the rows sit in plain columns with no encryption and no AAD. The day key
is the caller's to format (a local YYYY-MM-DD), so every consumer agrees
on what "today" means through one helper instead of three.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger("aether.counters")

# the columns the vocabulary accepts — the SQL is always formed from this
# set, never from a caller's string
FIELDS = frozenset(
    {"searches", "tasks", "tokens", "sandbox_seconds", "notifications", "images", "sandbox_jobs"}
)

# guards against unbounded growth the same way every other time-scoped
# store does: rows older than a year are pruned on bump, not on a timer


class CounterError(ValueError):
    """A field or value the counter vocabulary refuses."""


def today(now: datetime | None = None) -> str:
    """The day key for a moment — YYYY-MM-DD in the caller's local time."""
    return (now or datetime.now()).strftime("%Y-%m-%d")


class DailyCounters:
    """Counters in Postgres — a one-step numeric upsert per bump."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def bump(self, day: str, field: str, n: int = 1) -> int:
        """Add n to one day's count and return the new value. First bump
        of a day creates its row; the insert and the increment are one
        statement, so concurrent bumps cannot lose a count."""
        if field not in FIELDS:
            raise CounterError(f"unknown counter {field!r}")
        if n < 0:
            raise CounterError("a counter only goes up")
        row = await self._pool.fetchrow(
            f"INSERT INTO daily_counters (day, {field})"  # field is from FIELDS
            " VALUES ($1, $2)"
            " ON CONFLICT (day) DO UPDATE SET"
            f" {field} = daily_counters.{field} + EXCLUDED.{field},"
            " updated_at = now()"
            f" RETURNING {field}",
            day,
            n,
        )
        value = int(row[field])
        if value == n:  # the row was just created — piggyback the prune
            await self._prune(day)
        return value

    async def value(self, day: str, field: str) -> int:
        """One day's current count — 0 when the day has no row yet."""
        if field not in FIELDS:
            raise CounterError(f"unknown counter {field!r}")
        row = await self._pool.fetchrow(f"SELECT {field} FROM daily_counters WHERE day = $1", day)
        return int(row[field]) if row else 0

    async def _prune(self, day: str) -> None:
        """Drop rows older than a year, best-effort — a failed prune never
        fails the bump that triggered it."""
        try:
            year = int(day[:4]) - 1
            cutoff = day.replace(day[:4], f"{year:04d}", 1)
            await self._pool.execute("DELETE FROM daily_counters WHERE day < $1", cutoff)
        except Exception:  # noqa: BLE001 — house rule: never crash on cleanup
            log.warning("daily counters prune failed", exc_info=True)
