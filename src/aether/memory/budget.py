"""A process-local spend guard for optional background database work.

Only optional work asks: the vector backfill pauses for the rest of the UTC
day when the configured daily totals are already spent. Interactive
messages, polls and panel reads never consult it, so a budget can never
delay a reply.

The numbers are the same in-process estimates the panel shows, not a reading
of the provider's meter, and the guard holds nothing user-authored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DailyBudget:
    """The remaining allowance for optional work, from the live meters."""

    accounting: Any
    max_bytes: int
    max_queries: int

    def _used(self) -> tuple[int, int]:
        if self.accounting is None:
            return 0, 0
        used = self.accounting.today()
        return int(used.get("calls", 0)), int(used.get("bytes", 0))

    def can_spend(self, *, queries: int = 1, bytes_estimate: int = 0) -> bool:
        calls, size = self._used()
        return calls + queries <= self.max_queries and size + bytes_estimate <= self.max_bytes

    def remaining(self) -> dict[str, int]:
        calls, size = self._used()
        return {
            "queries_left": max(0, self.max_queries - calls),
            "bytes_left": max(0, self.max_bytes - size),
        }
