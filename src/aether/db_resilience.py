"""A circuit breaker for the one shared database pool.

An outage must not become a retry storm: after a few connection failures the
breaker opens, and every database call fails immediately with
`DatabaseUnavailable` until one probe succeeds. The breaker holds no user
data, makes no queries of its own, and is memory-only.

A SQL error is the code's mistake, not an outage, so it never opens the
circuit. Connection refusals, dropped connections, a closed pool and
timeouts do.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import asyncpg

log = logging.getLogger("aether.db")


class DatabaseUnavailable(RuntimeError):
    """The database was unreachable moments ago; the circuit is open."""


def is_connection_failure(exc: BaseException) -> bool:
    """True for failures a second attempt would only repeat."""
    if isinstance(exc, DatabaseUnavailable):
        return False
    if isinstance(
        exc,
        (
            asyncpg.exceptions.PostgresConnectionError,
            asyncpg.exceptions.InterfaceError,
        ),
    ):
        return True
    return isinstance(exc, (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError))


class CircuitBreaker:
    """Closed, open and half-open around one pool.

    Closed: calls pass. After `failures` consecutive connection failures the
    circuit opens for a jittered wait that doubles on every failed probe, up
    to `max_seconds`. Open: `check` raises `DatabaseUnavailable` without
    touching the network. Half-open: one probe is allowed; its success
    closes the circuit, its failure reopens for longer.
    """

    def __init__(
        self,
        *,
        failures: int = 3,
        base_seconds: float = 2.0,
        max_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._threshold = max(1, int(failures))
        self._base = max(0.1, float(base_seconds))
        self._max = max(self._base, float(max_seconds))
        self._clock = clock
        self._jitter = jitter
        self._state = "closed"
        self._consecutive = 0
        self._opens = 0
        self._opened_at = 0.0
        self._retry_after = 0.0
        self._probing = False

    @property
    def state(self) -> str:
        """The live state; an expired open circuit reads as half-open."""
        if self._state == "open" and self._clock() - self._opened_at >= self._retry_after:
            return "half_open"
        return self._state

    @property
    def available(self) -> bool:
        return self.state != "open"

    @property
    def retry_in_seconds(self) -> float:
        if self.state != "open":
            return 0.0
        return max(0.0, self._retry_after - (self._clock() - self._opened_at))

    def check(self) -> None:
        """Gate one database call. Raises when the circuit is open."""
        state = self.state
        if state == "open":
            raise DatabaseUnavailable(
                "the database is unavailable; retrying in about "
                f"{self.retry_in_seconds:.0f} seconds"
            )
        if state == "half_open":
            if self._probing:
                raise DatabaseUnavailable("the database is still being tested")
            self._state = "half_open"
            self._probing = True

    def record_success(self) -> None:
        """One reachable call: close the circuit and reset the backoff."""
        was_open = self._state != "closed"
        self._consecutive = 0
        self._probing = False
        self._state = "closed"
        self._retry_after = 0.0
        self._opens = 0
        if was_open:
            log.info("database reachable again — the circuit is closed")

    def record_failure(self) -> None:
        """One connection failure: count it, then open with backoff."""
        self._probing = False
        self._consecutive += 1
        state = self.state
        if state == "half_open" or (state == "closed" and self._consecutive >= self._threshold):
            self._open()

    def record_cancelled(self) -> None:
        """A cancelled call says nothing about reachability; free the probe."""
        self._probing = False

    def _open(self) -> None:
        self._opens += 1
        wait = min(self._max, self._base * (2 ** (self._opens - 1)))
        self._retry_after = wait * (0.8 + 0.4 * self._jitter())
        self._state = "open"
        self._opened_at = self._clock()
        self._probing = False
        log.warning(
            "database unreachable after %d consecutive failure(s) — "
            "circuit open for about %.0f seconds",
            self._consecutive,
            self._retry_after,
        )

    def snapshot(self) -> dict[str, Any]:
        """A status read for the panel and `aether db-stats`; no secrets."""
        return {
            "state": self.state,
            "consecutive_failures": self._consecutive,
            "opens": self._opens,
            "retry_in_seconds": round(self.retry_in_seconds, 1),
        }
