"""The replay harness: safety guard, active-time model and one real scratch run."""

from __future__ import annotations

import os

import pytest

from aether.config import AppConfig
from aether.db_replay import (
    ReplayOptions,
    ReplaySafetyError,
    active_seconds,
    run_replay,
    scratch_database_name,
)


def test_scratch_guard_refuses_the_live_database_and_unnamed_ones() -> None:
    assert scratch_database_name("postgresql:///aether_scratch_ci") == "aether_scratch_ci"
    assert scratch_database_name("postgresql://localhost/aether_replay") == "aether_replay"
    with pytest.raises(ReplaySafetyError):
        scratch_database_name("postgresql:///aether")
    with pytest.raises(ReplaySafetyError):
        scratch_database_name("postgresql:///production")
    with pytest.raises(ReplaySafetyError):
        scratch_database_name("postgresql://localhost")


def test_active_time_is_the_union_of_query_windows() -> None:
    # one query then a full idle window: only the grace period is active
    assert active_seconds([10.0], 0, 1000, 300) == 300
    # a gap longer than the window separates two active periods
    assert active_seconds([0.0, 1000.0], 0, 2000, 300) == 600
    # continuous queries keep compute awake for the whole day
    assert active_seconds([float(second) for second in range(0, 86400, 30)], 0, 86400, 300) == 86400
    assert active_seconds([], 0, 1000, 300) == 0


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("AETHER_TEST_DATABASE_URL"), reason="needs a scratch database"
)
async def test_replay_reports_queries_bytes_and_cache_missing_reads() -> None:
    url = os.environ["AETHER_TEST_DATABASE_URL"]
    config = AppConfig()
    options = ReplayOptions(
        seed_events=8,
        seed_docs=2,
        seed_doc_pages=2,
        seed_commitments=2,
        seed_facts=2,
        seed_traces=2,
        seed_audit=2,
        seed_deliveries=2,
        messages=2,
        mails=1,
        panel_mode="off",
        tick_seconds=3600,
        scheduler_seconds=3600,
        restarts_per_day=1,
    )
    result = await run_replay(url, config, options)

    snapshot = result.snapshot
    assert snapshot["totals"]["calls"] > 100
    assert snapshot["totals"]["bytes"] > 0
    sites = {site["site"] for site in snapshot["sites"]}
    assert "aether.memory.vectors.VectorMemory.search_vector" in sites
    # the resident matrix loads once at boot in bounded batches; a recall
    # never re-reads the table to rank
    cache = next(
        site for site in snapshot["sites"] if site["site"].endswith("VectorMemory.load_cache")
    )
    assert sum(query["calls"] for query in cache["queries"]) == 1
    assert result.active_seconds > 0
    assert result.seed_bytes > 0
