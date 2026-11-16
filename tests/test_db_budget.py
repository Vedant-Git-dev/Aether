"""The optional-work budget: a day's measured use can pause the backfill.

Interactive work never consults the budget. These tests pin the day rollover,
the allowance check, and that an over-budget backfill performs no query at
all.
"""

from __future__ import annotations

from fakes import FakeEmbedder

from aether.db_accounting import DBAccounting
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


def test_the_meters_keep_a_utc_day_bucket() -> None:
    accounting = DBAccounting()
    accounting.record("SELECT 1", "x" * 100, 1, 0.0, at=DAY * 20 + 10)
    accounting.record("SELECT 1", "y" * 50, 1, 0.0, at=DAY * 21 + 5)

    assert accounting.today() == {"calls": 1, "bytes": 50}


def test_the_budget_stops_optional_work_once_the_day_is_spent() -> None:
    accounting = DBAccounting()
    budget = DailyBudget(accounting, max_queries=3, max_bytes=1_000)
    assert budget.can_spend(queries=1, bytes_estimate=100)

    accounting.record("SELECT 1", "x" * 1_000, 1, 0.0)

    assert budget.can_spend(queries=1, bytes_estimate=100) is False
    assert budget.remaining() == {"queries_left": 2, "bytes_left": 0}


async def test_an_over_budget_backfill_reads_nothing() -> None:
    pool = RecordingPool()
    accounting = DBAccounting()
    accounting.record("SELECT 1", "x" * 2_000, 1, 0.0)
    budget = DailyBudget(accounting, max_queries=100, max_bytes=1_000)
    vectors = VectorMemory(pool, Cipher.from_b64(generate_key_b64()), FakeEmbedder(), budget=budget)

    assert await vectors.backfill(50) is None
    assert pool.calls == 0


async def test_backfill_resumes_after_the_budget_resets() -> None:
    pool = RecordingPool()
    accounting = DBAccounting()
    accounting.record("SELECT 1", "x" * 2_000_000, 1, 0.0)
    budget = DailyBudget(accounting, max_queries=100, max_bytes=1_000_000)
    vectors = VectorMemory(pool, Cipher.from_b64(generate_key_b64()), FakeEmbedder(), budget=budget)

    assert await vectors.backfill(50) is None
    assert pool.calls == 0

    accounting.reset()  # the next UTC day, or a restart

    assert await vectors.backfill(50) is None
    assert pool.calls == 1
    assert "indexed_at IS NULL" in pool.queries[0]
