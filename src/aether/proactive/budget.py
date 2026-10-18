"""The proactive worker's daily budgets — the caps that keep the
background mode from ever spending more than its user allowed.

Counted against the shared daily counters (the same table the web search
cap uses), so chat and background draw on one honest ledger: tasks, tokens,
and sandbox seconds per local day. Exhausted is a skip, never a crash —
the cycle simply finds nothing allowed and comes back next interval.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..config import ProactiveConfig
from ..counters import today

log = logging.getLogger("aether.proactive")

# (counter field, config limit) for the count-shaped budgets
_COUNT_LIMITS = (
    ("tasks", "max_tasks_per_day"),
    ("tokens", "max_tokens_per_day"),
)


class DayCounters(Protocol):
    """The counter surface the budgets need — the same slice the web tools
    read, so the real store and a test double both satisfy it."""

    async def value(self, day: str, field: str) -> int: ...

    async def bump(self, day: str, field: str, n: int = 1) -> int: ...


class Budget:
    """One day's caps against the counters. All reads are live — a
    /config change to the limits applies from the next cycle."""

    def __init__(self, counters: DayCounters, config: ProactiveConfig) -> None:
        self._counters = counters
        self._config = config

    async def room(self, day: str | None = None) -> int:
        """How many more tasks may run today — zero the moment any budget
        is spent (fail-safe: a cycle never starts a task it can't pay
        for), otherwise the task cap's remaining gap."""
        if await self.exhausted(day) is not None:
            return 0
        day = day or today()
        tasks = await self._counters.value(day, "tasks")
        return max(0, self._config.max_tasks_per_day - tasks)

    async def exhausted(self, day: str | None = None) -> str | None:
        """Which budget is spent, in plain words — None when there's room.
        The worker logs this once per skipped cycle, not per task."""
        day = day or today()
        for counter, limit in _COUNT_LIMITS:
            if await self._counters.value(day, counter) >= getattr(self._config, limit):
                return f"today's {counter.removesuffix('s').replace('_', ' ')} budget"
        seconds = await self._counters.value(day, "sandbox_seconds")
        if seconds >= self._config.max_sandbox_minutes_per_day * 60:
            return "today's sandbox time"
        return None

    async def notify_room(self, day: str | None = None) -> bool:
        """True when today's result-notification cap still has room — the
        same live-read discipline as every other budget, so a chat-made
        change to the cap applies from the next cycle."""
        day = day or today()
        return await self._counters.value(day, "notifications") < self._config.notify_daily_cap

    async def charge(
        self,
        day: str | None = None,
        *,
        tasks: int = 0,
        tokens: int = 0,
        sandbox_seconds: int = 0,
        notifications: int = 0,
    ) -> None:
        """Record what one finished task spent. Zero-cost parts of a task
        charge nothing — a skill that ran no sandbox job spends no time."""
        day = day or today()
        if tasks:
            await self._counters.bump(day, "tasks", tasks)
        if tokens:
            await self._counters.bump(day, "tokens", tokens)
        if sandbox_seconds:
            await self._counters.bump(day, "sandbox_seconds", sandbox_seconds)
        if notifications:
            await self._counters.bump(day, "notifications", notifications)
