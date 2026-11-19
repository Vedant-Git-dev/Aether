"""The outage circuit: open after repeated connection failures, fail fast,
probe once, close on success, and never trip on a statement error."""

from __future__ import annotations

from typing import Any

import pytest

from aether.db_accounting import DBAccounting, MeteredPool
from aether.db_resilience import (
    CircuitBreaker,
    DatabaseUnavailable,
    is_connection_failure,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FailingPool:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def fetch(self, query: str, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _breaker(clock: FakeClock, **kwargs) -> CircuitBreaker:
    defaults = {
        "failures": 3,
        "base_seconds": 2.0,
        "max_seconds": 300.0,
        "clock": clock,
        "jitter": lambda: 0.5,  # factor 1.0: the waits are exact in tests
    }
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


def test_the_threshold_opens_the_circuit_and_calls_fail_fast() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)

    for _ in range(2):
        breaker.record_failure()
    assert breaker.available

    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.available is False
    with pytest.raises(DatabaseUnavailable):
        breaker.check()


def test_one_probe_is_allowed_and_success_closes_the_circuit() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()

    clock.now = 2.0
    assert breaker.state == "half_open"
    breaker.check()  # the one probe
    with pytest.raises(DatabaseUnavailable):
        breaker.check()  # a second caller does not probe in parallel

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.available
    breaker.check()  # calls pass again


def test_a_failed_probe_doubles_the_wait_up_to_the_max() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, failures=1, base_seconds=2.0, max_seconds=5.0)

    breaker.record_failure()
    assert breaker.retry_in_seconds == 2.0

    clock.now = 2.0
    breaker.check()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.retry_in_seconds == 4.0

    clock.now = 6.0
    breaker.check()
    breaker.record_failure()
    assert breaker.retry_in_seconds == 5.0  # capped


def test_a_statement_error_is_not_a_connection_failure() -> None:
    assert is_connection_failure(ValueError("bad sql")) is False
    assert is_connection_failure(ConnectionRefusedError()) is True
    assert is_connection_failure(TimeoutError()) is True


async def test_an_open_circuit_stops_before_the_network() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, failures=2, base_seconds=60.0)
    failing = FailingPool(ConnectionRefusedError("no route"))
    pool = MeteredPool(failing, DBAccounting(), breaker=breaker)

    for _ in range(2):
        with pytest.raises(ConnectionRefusedError):
            await pool.fetch("SELECT 1")
    assert failing.calls == 2
    assert breaker.state == "open"

    with pytest.raises(DatabaseUnavailable):
        await pool.fetch("SELECT 1")
    assert failing.calls == 2  # no new attempt reached the pool


async def test_a_statement_error_leaves_the_circuit_closed() -> None:
    breaker = _breaker(FakeClock())
    pool = MeteredPool(FailingPool(ValueError("syntax")), DBAccounting(), breaker=breaker)

    for _ in range(5):
        with pytest.raises(ValueError):
            await pool.fetch("SELECT 1")

    assert breaker.available
    assert breaker.snapshot()["consecutive_failures"] == 0


class _Lease:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _LeasePool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> _Lease:
        return _Lease(self.conn)


class _HealthyConnection:
    async def fetch(self, query: str, *args, **kwargs):
        return [{"ok": 1}]


async def test_a_lease_closes_the_circuit_only_after_its_query_succeeds() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, failures=1, base_seconds=10.0)
    pool = MeteredPool(_LeasePool(_HealthyConnection()), DBAccounting(), breaker=breaker)

    breaker.record_failure()
    clock.now = 10.0  # the wait is over: half-open

    async with pool.acquire() as conn:
        # holding a lease proves nothing yet: an idle connection may be
        # reused without the server answering
        assert breaker.state == "half_open"
        await conn.fetch("SELECT 1")

    assert breaker.state == "closed"


async def test_an_unused_lease_frees_the_probe_for_the_next_caller() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, failures=1, base_seconds=10.0)
    pool = MeteredPool(_LeasePool(_HealthyConnection()), DBAccounting(), breaker=breaker)

    breaker.record_failure()
    clock.now = 10.0
    async with pool.acquire():
        pass  # no query: the probe was not spent

    assert breaker.state == "half_open"
    breaker.check()  # the next caller may probe
