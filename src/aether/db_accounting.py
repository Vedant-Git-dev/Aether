"""Per-query database accounting — counts, rows, response bytes and call site.

The database is a metered resource, so every query is measured: which code
asked, how many rows and how many bytes came back, how long it took, and when
it happened. The meters live in process memory only. They carry counts and
query shapes, never query parameters, row values or any user content, so the
"no user data unencrypted" rule holds without touching the database.

`MeteredPool` wraps an asyncpg pool and is transparent to every caller:
stores keep using `fetch`, `fetchrow`, `fetchval`, `execute` and
`async with pool.acquire()`. Response bytes are estimated from the returned
records (scalar widths plus payload lengths), not read from the wire, and the
report labels them as estimates.

The snapshot is served by the panel's `/api/db/stats` route and rendered by
`aether db-stats`. Both read the live process's counters; a separate process
cannot see them, so the CLI talks to the running server over localhost.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from .db_resilience import CircuitBreaker, is_connection_failure

log = logging.getLogger("aether.db")

# how many bytes one plain scalar is assumed to cost on the wire — a
# conservative fixed width for timestamps, numbers and booleans. Lengths are
# exact for text and bytes; this is the estimate's only approximation.
_SCALAR_BYTES = 8

_KIND_RE = re.compile(r"^\s*(?:/\*.*?\*/\s*)*([a-z]+)", re.IGNORECASE | re.DOTALL)
_TABLE_RE = re.compile(
    r"\b(?:from|into|update|join)\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

# frame → "module.function"; code objects are stable for the process, so the
# name is resolved once per code object rather than on every query
_SITE_CACHE: dict[int, str] = {}


def estimate_bytes(value: Any) -> int:
    """Approximate one result value's wire size.

    text and bytes are exact; numbers, dates and booleans take a fixed scalar
    width; containers add up. Parameters never travel through here, so nothing
    user-authored is ever held by the accounting layer.
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


def query_shape(sql: str) -> tuple[str, str]:
    """(verb, first table) for one statement — a stable label, no parameters."""
    text = sql or ""
    kind_match = _KIND_RE.match(text)
    kind = kind_match.group(1).lower() if kind_match else "other"
    table_match = _TABLE_RE.search(text)
    table = table_match.group(1).lower() if table_match else ""
    return kind, table


def _callsite() -> str:
    """The first application frame above the accounting wrapper."""
    frame = sys._getframe(2)
    fallback = "unknown"
    while frame is not None:
        code = frame.f_code
        cached = _SITE_CACHE.get(id(code))
        if cached is not None:
            return cached
        module = frame.f_globals.get("__name__", "")
        if module and module != __name__ and not module.startswith("asyncpg"):
            site = f"{module}.{code.co_qualname}"
            if module.startswith("aether."):
                _SITE_CACHE[id(code)] = site
                return site
            if fallback == "unknown":
                fallback = site
        frame = frame.f_back
    return fallback


@dataclass
class QueryStat:
    calls: int = 0
    rows: int = 0
    bytes: int = 0
    seconds: float = 0.0

    def add(self, rows: int, size: int, seconds: float) -> None:
        self.calls += 1
        self.rows += rows
        self.bytes += size
        self.seconds += seconds


@dataclass
class SiteStat:
    calls: int = 0
    rows: int = 0
    bytes: int = 0
    seconds: float = 0.0
    queries: dict[tuple[str, str], QueryStat] = field(default_factory=dict)

    def add(self, kind: str, table: str, rows: int, size: int, seconds: float) -> None:
        self.calls += 1
        self.rows += rows
        self.bytes += size
        self.seconds += seconds
        self.queries.setdefault((kind, table), QueryStat()).add(rows, size, seconds)


class DBAccounting:
    """In-memory meters: totals, per call site, per table and per hour."""

    def __init__(
        self, *, enabled: bool = True, clock=time.time, collect_timeline: bool = False
    ) -> None:
        self._enabled = enabled
        self._clock = clock
        self._collect_timeline = collect_timeline
        self._timeline: list[float] = []
        self._started_at = clock()
        self._lock = Lock()
        self._sites: dict[str, SiteStat] = {}
        self._tables: dict[str, QueryStat] = {}
        self._hourly = [0] * 24
        # the current UTC day's counters, for optional-work budgets. The day
        # is a whole number of seconds since the epoch, so the rollover check
        # is an integer compare on every query.
        self._today_index = -1
        self._today_calls = 0
        self._today_bytes = 0
        # connection bookkeeping — acquisitions, peak concurrent leases, and
        # the pool's own view of its size when one is attached
        self._acquires = 0
        self._active = 0
        self._peak_active = 0
        self._pool: Any = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def attach_pool(self, pool: Any) -> None:
        self._pool = pool

    def record(
        self,
        sql: str,
        result: Any,
        rows: int,
        seconds: float,
        *,
        site: str | None = None,
        at: float | None = None,
    ) -> None:
        if not self._enabled:
            return
        kind, table = query_shape(sql)
        size = estimate_bytes(result)
        moment = self._clock() if at is None else at
        hour = datetime.fromtimestamp(moment, UTC).hour
        origin = site or _callsite()
        today = int(moment // 86_400)
        with self._lock:
            self._sites.setdefault(origin, SiteStat()).add(kind, table, rows, size, seconds)
            self._tables.setdefault(table or "(no table)", QueryStat()).add(rows, size, seconds)
            self._hourly[hour] += 1
            if today != self._today_index:
                self._today_index = today
                self._today_calls = 0
                self._today_bytes = 0
            self._today_calls += 1
            self._today_bytes += size
            if self._collect_timeline:
                self._timeline.append(moment)

    def acquired(self) -> None:
        with self._lock:
            self._acquires += 1
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)

    def released(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _pool_state(self) -> dict[str, int]:
        pool = self._pool
        out: dict[str, int] = {}
        if pool is None:
            return out
        for name in ("get_size", "get_idle_size", "get_min_size", "get_max_size"):
            getter = getattr(pool, name, None)
            if getter is None:
                continue
            try:
                out[name.removeprefix("get_")] = int(getter())
            except Exception:  # noqa: BLE001 — a closed pool is a report detail
                continue
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sites = [
                {
                    "site": site,
                    "calls": stat.calls,
                    "rows": stat.rows,
                    "bytes": stat.bytes,
                    "seconds": round(stat.seconds, 4),
                    "queries": [
                        {
                            "kind": kind,
                            "table": table,
                            "calls": query.calls,
                            "rows": query.rows,
                            "bytes": query.bytes,
                        }
                        for (kind, table), query in sorted(
                            stat.queries.items(), key=lambda item: item[1].bytes, reverse=True
                        )
                    ],
                }
                for site, stat in self._sites.items()
            ]
            sites.sort(key=lambda item: (item["bytes"], item["calls"]), reverse=True)
            tables = [
                {
                    "table": table,
                    "calls": stat.calls,
                    "rows": stat.rows,
                    "bytes": stat.bytes,
                }
                for table, stat in sorted(
                    self._tables.items(), key=lambda item: item[1].bytes, reverse=True
                )
            ]
            now = self._clock()
            return {
                "ok": True,
                "enabled": self._enabled,
                "since": datetime.fromtimestamp(self._started_at, UTC).isoformat(),
                "uptime_seconds": round(now - self._started_at, 1),
                "totals": {
                    "calls": sum(site["calls"] for site in sites),
                    "rows": sum(site["rows"] for site in sites),
                    "bytes": sum(site["bytes"] for site in sites),
                    "seconds": round(sum(site["seconds"] for site in sites), 3),
                },
                "sites": sites,
                "tables": tables,
                "hourly": list(self._hourly),
                "connections": {
                    "acquires": self._acquires,
                    "active": self._active,
                    "peak": self._peak_active,
                    **self._pool_state(),
                },
            }

    def today(self) -> dict[str, int]:
        """Queries and estimated bytes recorded for the current UTC day."""
        with self._lock:
            return {"calls": self._today_calls, "bytes": self._today_bytes}

    def reset(self) -> None:
        with self._lock:
            self._sites.clear()
            self._tables.clear()
            self._hourly = [0] * 24
            self._today_index = -1
            self._today_calls = 0
            self._today_bytes = 0
            self._timeline.clear()
            self._acquires = 0
            self._active = 0
            self._peak_active = 0
            self._started_at = self._clock()

    def timeline(self) -> list[float]:
        """The timestamps of every recorded query (harness mode only)."""
        with self._lock:
            return list(self._timeline)


DEFAULT_ACCOUNTING = DBAccounting()


def _row_count(result: Any) -> int:
    if result is None:
        return 0
    if isinstance(result, (list, tuple)):
        return len(result)
    if isinstance(result, (str, bytes, bytearray, memoryview)):
        return 0
    return 1


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
    accounting: DBAccounting,
    breaker: CircuitBreaker | None,
    query: str,
    call: Callable[[], Any],
    *,
    rows: Callable[[Any], int] = _row_count,
) -> Any:
    """Run one query, record it, and tell the breaker how it went."""
    started = time.perf_counter()
    try:
        result = await call()
    except BaseException as exc:
        accounting.record(query, None, 0, time.perf_counter() - started)
        _observe(breaker, exc)
        raise
    accounting.record(query, result, rows(result), time.perf_counter() - started)
    _observe(breaker, None)
    return result


class _MeteredConnection:
    """Connection proxy: the same methods, measured, everything else delegated."""

    def __init__(
        self, conn: Any, accounting: DBAccounting, breaker: CircuitBreaker | None = None
    ) -> None:
        self._conn = conn
        self._accounting = accounting
        self._breaker = breaker

    async def _call(self, query: str, call: Callable[[], Any], **kwargs: Any) -> Any:
        return await _measured(self._accounting, self._breaker, query, call, **kwargs)

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(query, lambda: self._conn.fetch(query, *args, **kwargs))

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(query, lambda: self._conn.fetchrow(query, *args, **kwargs))

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(
            query,
            lambda: self._conn.fetchval(query, *args, **kwargs),
            rows=lambda value: 0 if value is None else 1,
        )

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call(
            query, lambda: self._conn.execute(query, *args, **kwargs), rows=lambda _: 0
        )

    async def executemany(self, query: str, args: Any, **kwargs: Any) -> Any:
        return await self._call(
            query, lambda: self._conn.executemany(query, args, **kwargs), rows=lambda _: 0
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _Acquire:
    """`pool.acquire()` proxy that measures the lease and wraps the connection."""

    def __init__(
        self,
        pool: Any,
        context: Any,
        accounting: DBAccounting,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._pool = pool
        self._context = context
        self._accounting = accounting
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
        self._accounting.acquired()
        return _MeteredConnection(conn, self._accounting, self._breaker)

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            return await self._context.__aexit__(*exc)
        finally:
            self._accounting.released()
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
        accounting: DBAccounting | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._pool = pool
        self.accounting = accounting or DBAccounting()
        self.accounting.attach_pool(pool)
        self.breaker = breaker

    async def _run(self, method: str, query: str, args: tuple, kwargs: dict) -> Any:
        if self.breaker is not None:
            self.breaker.check()
        return await _measured(
            self.accounting,
            self.breaker,
            query,
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
        return _Acquire(self, self._pool.acquire(*args, **kwargs), self.accounting, self.breaker)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)
