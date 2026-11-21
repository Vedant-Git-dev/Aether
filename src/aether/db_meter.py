"""A process-local meter for database queries.

Every query through `MeteredPool` adds one call and an estimated response
size to the current UTC day's bucket. Only optional background work reads the
totals — the vector backfill pauses once its budget is spent — so the meter
can never delay an interactive message. It holds counts and byte estimates,
never query parameters, rows or any user content, so the "no user data
unencrypted" rule holds without touching the database.

`MeteredPool` wraps an asyncpg pool and is transparent to every caller:
stores keep using `fetch`, `fetchrow`, `fetchval`, `execute` and
`async with pool.acquire()`. Response bytes are estimated from the returned
records (scalar widths plus payload lengths), not read from the wire.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from .db_resilience import CircuitBreaker, is_connection_failure

# how many bytes one plain scalar is assumed to cost on the wire — a
# conservative fixed width for timestamps, numbers and booleans. Lengths are
# exact for text and bytes; this is the estimate's only approximation.
_SCALAR_BYTES = 8

DAY_SECONDS = 86_400


def estimate_bytes(value: Any) -> int:
    """Approximate one result value's wire size.

    text and bytes are exact; numbers, dates and booleans take a fixed scalar
    width; containers add up. Parameters never travel through here, so nothing
    user-authored is ever held by the meter.
    """
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    if isinstance(value, Mapping):
        return sum(estimate_bytes(key) + estimate_bytes(item) for key, item in value.items())
    # asyncpg.Record is a mapping-like row but not a collections.abc.Mapping
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return sum(estimate_bytes(key) + estimate_bytes(item) for key, item in items())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(estimate_bytes(item) for item in value)
    if isinstance(value, bool):
        return 1
    return _SCALAR_BYTES


class QueryMeter:
    """The current UTC day's call and estimated-byte totals, in memory."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = Lock()
        # the day is a whole number of seconds since the epoch, so the
        # rollover check is an integer compare on every query
        self._day = -1
        self._calls = 0
        self._bytes = 0

    def _roll(self) -> None:
        day = int(self._clock() // DAY_SECONDS)
        if day != self._day:
            self._day = day
            self._calls = 0
            self._bytes = 0

    def record(self, size: int) -> None:
        with self._lock:
            self._roll()
            self._calls += 1
            self._bytes += max(0, int(size))

    def today(self) -> dict[str, int]:
        """Queries and estimated bytes recorded for the current UTC day."""
        with self._lock:
            self._roll()
            return {"calls": self._calls, "bytes": self._bytes}


def _observe(breaker: CircuitBreaker | None, exc: BaseException | None) -> None:
    """Tell the breaker what one call proved about reachability."""
    if breaker is None:
        return
    if exc is None:
        breaker.record_success()
    elif isinstance(exc, asyncio.CancelledError):
        breaker.record_cancelled()
    elif is_connection_failure(exc):
        breaker.record_failure()
    else:
        # the server answered, so the connection is healthy even when the
        # statement itself was wrong
        breaker.record_success()


async def _measured(
    meter: QueryMeter,
    breaker: CircuitBreaker | None,
    call: Callable[[], Any],
) -> Any:
    """Run one query, record it, and tell the breaker how it went."""
    try:
        result = await call()
    except BaseException as exc:
        meter.record(0)
        _observe(breaker, exc)
        raise
    meter.record(estimate_bytes(result))
    _observe(breaker, None)
    return result


class _MeteredConnection:
    """Connection proxy: the same methods, measured, everything else delegated."""

    def __init__(self, conn: Any, meter: QueryMeter, breaker: CircuitBreaker | None = None) -> None:
        self._conn = conn
        self._meter = meter
        self._breaker = breaker

    async def _call(self, call: Callable[[], Any]) -> Any:
        return await _measured(self._meter, self._breaker, call)

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(lambda: self._conn.fetch(query, *args, **kwargs))

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(lambda: self._conn.fetchrow(query, *args, **kwargs))

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(lambda: self._conn.fetchval(query, *args, **kwargs))

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(lambda: self._conn.execute(query, *args, **kwargs))

    async def executemany(self, query: str, args: Any, **kwargs: Any) -> Any:
        return await self._call(lambda: self._conn.executemany(query, args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _Acquire:
    """`pool.acquire()` proxy that measures the lease and wraps the connection."""

    def __init__(
        self,
        pool: Any,
        context: Any,
        meter: QueryMeter,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._pool = pool
        self._context = context
        self._meter = meter
        self._breaker = breaker

    async def __aenter__(self) -> _MeteredConnection:
        if self._breaker is not None:
            self._breaker.check()
        try:
            conn = await self._context.__aenter__()
        except BaseException as exc:
            _observe(self._breaker, exc)
            raise
        # a lease is not proof the server answers: the first query on the
        # connection reports that. Releasing the lease without one frees
        # the probe so the next caller can try.
        return _MeteredConnection(conn, self._meter, self._breaker)

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            return await self._context.__aexit__(*exc)
        finally:
            if self._breaker is not None:
                self._breaker.record_cancelled()


class MeteredPool:
    """An asyncpg pool with every query measured; a transparent stand-in.

    `breaker`, when given, gates every call: while the circuit is open the
    pool raises `DatabaseUnavailable` immediately instead of waiting on a
    network timeout, which is what keeps an outage from becoming a retry
    storm.
    """

    def __init__(
        self,
        pool: Any,
        meter: QueryMeter | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._pool = pool
        self.meter = meter or QueryMeter()
        self.breaker = breaker

    async def _run(self, method: str, query: str, args: tuple, kwargs: dict) -> Any:
        if self.breaker is not None:
            self.breaker.check()
        return await _measured(
            self.meter,
            self.breaker,
            lambda: getattr(self._pool, method)(query, *args, **kwargs),
        )

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetch", query, args, kwargs)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetchrow", query, args, kwargs)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("fetchval", query, args, kwargs)

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._run("execute", query, args, kwargs)

    async def executemany(self, query: str, args: Any, **kwargs: Any) -> Any:
        return await self._run("executemany", query, (args,), kwargs)

    def acquire(self, *args: Any, **kwargs: Any) -> _Acquire:
        return _Acquire(self, self._pool.acquire(*args, **kwargs), self.meter, self.breaker)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)
