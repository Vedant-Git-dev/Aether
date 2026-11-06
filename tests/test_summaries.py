"""Summaries: sealed rows, in-place upsert, deterministic scheduling.

A summary is one row per (kind, period), so refreshing a thread or a day must
replace rather than duplicate. Generation is a model call behind
`memory.summaries`; the scheduler decides what is due and the store seals
what comes back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.summaries import (
    DAILY,
    THREAD,
    WEEKLY,
    Summaries,
    SummaryJob,
    day_key,
    week_key,
)

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
SECRET = "AETHER-SUMMARY-CANARY-77aa"


def _store(db) -> Summaries:
    return Summaries(db, Cipher.from_b64(generate_key_b64()))


class StubSummarizer:
    def __init__(self, text: str = "the day in one line") -> None:
        self.text = text
        self.calls: list[tuple[str, str, list[str]]] = []

    async def summarize(self, kind: str, period: str, items: list[str]) -> str:
        self.calls.append((kind, period, list(items)))
        return self.text


class FakeEvents:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self.events = events

    async def recent(self, limit: int = 50) -> list[SimpleNamespace]:
        return self.events[:limit]


def _event(
    event_id: int,
    when: datetime,
    *,
    summary: str = "something happened",
    thread_id: str = "",
    noise: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id,
        occurred_at=when,
        noise=noise,
        summary=summary,
        payload={"thread_id": thread_id} if thread_id else {},
    )


def test_period_keys_use_the_users_calendar() -> None:
    # 2026-11-03 06:16 UTC is 11:46 in IST, still 3 November
    assert day_key(NOW, "Asia/Kolkata") == "2026-11-03"
    assert week_key(NOW, "Asia/Kolkata").startswith("2026-W")


@pytest.mark.integration
async def test_a_summary_round_trips(db) -> None:
    summaries = _store(db)

    written = await summaries.upsert(DAILY, "2026-11-03", "the day in one line", [1, 2], now=NOW)

    assert written is not None
    found = await summaries.get(DAILY, "2026-11-03")
    assert found is not None
    assert found.text == "the day in one line"
    assert found.source_event_ids == (1, 2)


@pytest.mark.integration
async def test_the_body_is_ciphertext_at_rest(db) -> None:
    summaries = _store(db)
    await summaries.upsert(DAILY, "2026-11-03", SECRET, [1], now=NOW)

    blob = await db.fetchval("SELECT body_enc FROM summaries")
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob
    columns = {
        row["column_name"]
        for row in await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'summaries'"
        )
    }
    assert not {"text", "body", "source_event_ids"} & columns


@pytest.mark.integration
async def test_upsert_replaces_one_row_per_period(db) -> None:
    summaries = _store(db)
    await summaries.upsert(DAILY, "2026-11-03", "first", [1], now=NOW)
    await summaries.upsert(DAILY, "2026-11-03", "second", [1, 2], now=NOW)

    found = await summaries.recent(DAILY)
    assert len(found) == 1
    assert found[0].text == "second"
    assert found[0].source_event_ids == (1, 2)


@pytest.mark.integration
async def test_an_empty_summary_is_not_stored(db) -> None:
    summaries = _store(db)

    assert await summaries.upsert(DAILY, "2026-11-03", "  ", [1], now=NOW) is None
    assert await summaries.upsert(DAILY, "2026-11-03", "x", [1], now=NOW) is not None
    assert await db.fetchval("SELECT COUNT(*) FROM summaries") == 1


@pytest.mark.integration
async def test_the_job_writes_one_due_daily_summary(db) -> None:
    summaries = _store(db)
    summarizer = StubSummarizer()
    yesterday = NOW - timedelta(days=1)
    job = SummaryJob(
        events=FakeEvents([_event(1, yesterday, summary="a"), _event(2, yesterday, summary="b")]),
        summaries=summaries,
        summarizer=summarizer,
        memory=SimpleNamespace(summaries=True, summary_thread_messages=10),
        clock=lambda: NOW,
    )

    written = await job.tick(NOW)

    assert written == 1
    found = await summaries.get(DAILY, day_key(yesterday))
    assert found is not None and found.text == summarizer.text
    # the second pass finds it and writes nothing
    assert await job.tick(NOW) == 0


@pytest.mark.integration
async def test_a_thread_refreshes_only_after_the_threshold(db) -> None:
    summaries = _store(db)
    summarizer = StubSummarizer()
    events = [
        _event(1, NOW, summary="first", thread_id="thread-9"),
        _event(2, NOW, summary="second", thread_id="thread-9"),
    ]
    job = SummaryJob(
        events=FakeEvents(events),
        summaries=summaries,
        summarizer=summarizer,
        memory=SimpleNamespace(summaries=True, summary_thread_messages=2),
        clock=lambda: NOW,
    )

    assert await job.tick(NOW) == 1
    found = await summaries.get(THREAD, "thread-9")
    assert found is not None and found.source_event_ids == (1, 2)
    # no new messages since: nothing to refresh
    assert await job.tick(NOW) == 0


@pytest.mark.integration
async def test_the_job_writes_the_previous_week(db) -> None:
    summaries = _store(db)
    summarizer = StubSummarizer()
    last_week = NOW - timedelta(days=7)
    job = SummaryJob(
        events=FakeEvents([_event(1, last_week, summary="a"), _event(2, last_week, summary="b")]),
        summaries=summaries,
        summarizer=summarizer,
        memory=SimpleNamespace(summaries=True, summary_thread_messages=10),
        clock=lambda: NOW,
    )

    assert await job.tick(NOW) == 1
    assert await summaries.get(WEEKLY, week_key(last_week)) is not None


@pytest.mark.integration
async def test_generation_is_off_unless_enabled(db) -> None:
    summaries = _store(db)
    summarizer = StubSummarizer()
    yesterday = NOW - timedelta(days=1)
    job = SummaryJob(
        events=FakeEvents([_event(1, yesterday), _event(2, yesterday)]),
        summaries=summaries,
        summarizer=summarizer,
        memory=SimpleNamespace(summaries=False, summary_thread_messages=10),
        clock=lambda: NOW,
    )

    assert await job.tick(NOW) == 0
    assert summarizer.calls == []
