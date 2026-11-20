"""The optional-work budget: a day's measured use can pause the backfill.

Interactive work never consults the budget. These tests pin the day rollover,
the allowance check, and that an over-budget backfill performs no query at
all.
"""

from __future__ import annotations

from fakes import FakeEmbedder

from aether.db_meter import MeteredPool, QueryMeter
from aether.memory.budget import DailyBudget
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.vectors import VectorMemory

DAY = 86_400


class RecordingPool:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.calls = 0

    async def fetch(self, query: str, *args, **kwargs):
        self.queries.append(query)
        self.calls += 1
        return []

    async def execute(self, query: str, *args, **kwargs) -> str:
        self.queries.append(query)
        self.calls += 1
        return "OK"


async def test_the_pool_meters_each_query() -> None:
    class QueryPool:
        async def fetch(self, query: str, *args, **kwargs):
            return ["data"]

    meter = QueryMeter()
    pool = MeteredPool(QueryPool(), meter)

    await pool.fetch("SELECT 1")

    assert meter.today() == {"calls": 1, "bytes": 4}


def test_the_meter_keeps_a_utc_day_bucket() -> None:
    now = [DAY * 20 + 10]
    meter = QueryMeter(clock=lambda: now[0])
    meter.record(100)
    now[0] = DAY * 21 + 5
    meter.record(50)

    assert meter.today() == {"calls": 1, "bytes": 50}


def test_a_new_day_clears_the_bucket_without_a_query() -> None:
    # the budget asks before the backfill runs, so the rollover must not
    # depend on some other query arriving first
    now = [DAY * 20 + 10]
    meter = QueryMeter(clock=lambda: now[0])
    meter.record(2_000)
    now[0] = DAY * 21 + 5

    assert meter.today() == {"calls": 0, "bytes": 0}


def test_the_budget_stops_optional_work_once_the_day_is_spent() -> None:
    meter = QueryMeter()
    budget = DailyBudget(meter, max_queries=3, max_bytes=1_000)
    assert budget.can_spend(queries=1, bytes_estimate=100)

    meter.record(1_000)

    assert budget.can_spend(queries=1, bytes_estimate=100) is False


async def test_an_over_budget_backfill_reads_nothing() -> None:
    pool = RecordingPool()
    meter = QueryMeter()
    meter.record(2_000)
    budget = DailyBudget(meter, max_queries=100, max_bytes=1_000)
    vectors = VectorMemory(pool, Cipher.from_b64(generate_key_b64()), FakeEmbedder(), budget=budget)

    assert await vectors.backfill(50) is None
    assert pool.calls == 0


async def test_backfill_resumes_after_the_budget_resets() -> None:
    now = [DAY * 20 + 10]
    pool = RecordingPool()
    meter = QueryMeter(clock=lambda: now[0])
    meter.record(2_000_000)
    budget = DailyBudget(meter, max_queries=100, max_bytes=1_000_000)
    vectors = VectorMemory(pool, Cipher.from_b64(generate_key_b64()), FakeEmbedder(), budget=budget)

    assert await vectors.backfill(50) is None
    assert pool.calls == 0

    now[0] += DAY  # the next UTC day

    assert await vectors.backfill(50) is None
    assert pool.calls == 1
    assert "indexed_at IS NULL" in pool.queries[0]
