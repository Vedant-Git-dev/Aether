"""Daily counters — the upsert-and-increment contract, against a real
Postgres (the integration fixture; skipped without AETHER_TEST_DATABASE_URL).
Counts only ever go up, a fresh day starts at zero, and the field
vocabulary refuses anything outside its set.
"""

from __future__ import annotations

import pytest

from aether.counters import CounterError, DailyCounters, today

pytestmark = pytest.mark.integration


async def test_bump_creates_the_day_and_increments(db) -> None:
    counters = DailyCounters(db)
    day = today()
    assert await counters.value(day, "searches") == 0
    assert await counters.bump(day, "searches") == 1
    assert await counters.bump(day, "searches") == 2
    assert await counters.bump(day, "searches", 3) == 5
    assert await counters.value(day, "searches") == 5


async def test_fields_do_not_bleed_into_each_other(db) -> None:
    counters = DailyCounters(db)
    day = today()
    await counters.bump(day, "searches", 4)
    await counters.bump(day, "tasks", 2)
    await counters.bump(day, "notifications")
    assert await counters.value(day, "searches") == 4
    assert await counters.value(day, "tasks") == 2
    assert await counters.value(day, "notifications") == 1
    assert await counters.value(day, "tokens") == 0


async def test_two_days_keep_separate_rows(db) -> None:
    counters = DailyCounters(db)
    await counters.bump("2026-01-01", "searches")
    await counters.bump("2026-01-02", "searches")
    assert await counters.value("2026-01-01", "searches") == 1
    assert await counters.value("2026-01-02", "searches") == 1


async def test_the_field_vocabulary_is_closed(db) -> None:
    counters = DailyCounters(db)
    with pytest.raises(CounterError):
        await counters.bump(today(), "not_a_field")
    with pytest.raises(CounterError):
        await counters.value(today(), "searches; DROP TABLE daily_counters")
    with pytest.raises(CounterError):
        await counters.bump(today(), "searches", -1)  # a counter only goes up
