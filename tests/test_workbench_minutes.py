"""The batch minutes meter — rounded-up charges and the cap, against a
real Postgres (the integration fixture; skipped without
AETHER_TEST_DATABASE_URL). Every charge becomes whole minutes, a fresh
month starts at zero, the warning line sits at 80%, and the cap refuses
the next job.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from aether.workbench.minutes import MinuteMeter, month

pytestmark = pytest.mark.integration


async def test_charges_round_up_to_whole_minutes(db) -> None:
    meter = MinuteMeter(db)
    # charge returns the month's new total, the way the daily counters do
    assert await meter.charge(30) == 1  # half a minute bills as one
    assert await meter.charge(61) == 3  # a minute and a second bills as two more
    assert await meter.charge(0) == 4  # even nothing is a started minute


async def test_a_fresh_month_starts_at_zero(db) -> None:
    meter = MinuteMeter(db)
    state = await meter.check(1500, now=datetime(2026, 10, 17, 9, 0))
    assert state.used_minutes == 0
    assert state.allowed
    assert not state.near_limit


async def test_the_cap_refuses_and_the_warning_line_sits_at_80_percent(db) -> None:
    meter = MinuteMeter(db)
    now = datetime(2026, 10, 17, 9, 0)
    await meter.charge(1200 * 60, now=now)  # 1200 of 1500 — the warning line
    state = await meter.check(1500, now=now)
    assert state.used_minutes == 1200
    assert state.near_limit
    assert state.allowed
    await meter.charge(300 * 60, now=now)  # exactly the cap — no more jobs
    state = await meter.check(1500, now=now)
    assert not state.allowed
    assert state.near_limit


async def test_months_keep_separate_rows(db) -> None:
    meter = MinuteMeter(db)
    await meter.charge(600, now=datetime(2026, 9, 30, 23, 0))
    await meter.charge(600, now=datetime(2026, 10, 1, 0, 0))
    september = await meter.check(1500, now=datetime(2026, 9, 30, 23, 0))
    october = await meter.check(1500, now=datetime(2026, 10, 1, 0, 0))
    assert september.used_minutes == 10
    assert october.used_minutes == 10


def test_the_month_key_is_plain_ym() -> None:
    assert month(datetime(2026, 10, 17, 9, 0)) == "2026-10"
    assert month() == datetime.now().strftime("%Y-%m")
