"""Idle discipline: the reads and writes a quiet Aether must not make.

These are offline tests with recording doubles. They pin the shapes that
Phase 1 and Phase 2 changed: recall ranks over a resident matrix and fetches
only the text it cites, trace summaries never decrypt a payload, the
scheduler asks a datetime instead of the database, and the panel source has
no timer at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeAudit, FakeEmbedder

from aether.agent.traces import Trace, Traces
from aether.llm.embeddings import pack_vector
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.vectors import VectorMemory
from aether.scheduler.jobs import Scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingPool:
    """A pool double that records every statement and replays canned rows."""

    def __init__(self, rows: list[list[dict]] | None = None) -> None:
        self.queries: list[str] = []
        self.calls = 0
        self._rows = list(rows or [])

    async def fetch(self, query: str, *args, **kwargs) -> list[dict]:
        self.queries.append(query)
        self.calls += 1
        return self._rows.pop(0) if self._rows else []

    async def fetchrow(self, query: str, *args, **kwargs) -> dict | None:
        self.queries.append(query)
        self.calls += 1
        return self._rows.pop(0)[0] if self._rows and self._rows[0] else None

    async def fetchval(self, query: str, *args, **kwargs):
        self.queries.append(query)
        self.calls += 1
        return 0

    async def execute(self, query: str, *args, **kwargs) -> str:
        self.queries.append(query)
        self.calls += 1
        return "OK"

    def acquire(self):  # pragma: no cover - not needed by these tests
        raise AssertionError("acquire() is not expected in these tests")


def _sealed_vector(cipher: Cipher, chunk_id: int, values: list[float]) -> bytes:
    return cipher.encrypt_bytes(pack_vector(values), aad=f"chunks:embedding_enc:{chunk_id}")


def _phantom_embedder() -> FakeEmbedder:
    return FakeEmbedder()


async def test_recall_scan_excludes_text_and_metadata_and_fetches_by_id() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    chunk_id = 7
    pool = RecordingPool(
        [
            [
                {
                    "id": chunk_id,
                    "event_id": 3,
                    "embedding_enc": _sealed_vector(cipher, chunk_id, [1.0, 0.0]),
                }
            ],
            [
                {
                    "id": chunk_id,
                    "text_enc": cipher.encrypt_text(
                        "the invoice is due", aad=f"chunks:text_enc:{chunk_id}"
                    ),
                    "meta_enc": cipher.encrypt_json(
                        {"source": "gmail", "kind": "mail", "location": ""},
                        aad=f"memory_chunks:meta_enc:{chunk_id}",
                    ),
                }
            ],
        ]
    )
    vectors = VectorMemory(pool, cipher, _phantom_embedder())
    hits = await vectors.search_vector([1.0, 0.0], limit=1, min_similarity=0.0)

    assert len(hits) == 1
    assert hits[0].text == "the invoice is due"
    assert hits[0].source == "gmail"
    assert len(pool.queries) == 2
    scan, detail = pool.queries
    assert "embedding_enc" in scan
    assert "text_enc" not in scan
    assert "meta_enc" not in scan
    assert "= ANY" in detail
    assert "text_enc" in detail and "meta_enc" in detail


async def test_cold_load_reads_each_row_once_within_the_batch_limit() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    rows = [
        {"id": index, "event_id": index, "embedding_enc": _sealed_vector(cipher, index, [1.0, 0.0])}
        for index in (3, 2, 1)
    ]
    pool = RecordingPool([rows[:2], rows[2:]])
    vectors = VectorMemory(pool, cipher, _phantom_embedder())

    await vectors.load_cache(batch=2, max_chunks=10)

    assert len(pool.queries) == 2
    assert all("embedding_enc" in query and "text_enc" not in query for query in pool.queries)
    assert len(vectors._cache_ids) == 3
    assert vectors._cache_watermark == 3


async def test_the_chunk_cap_keeps_the_newest() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    rows = [
        {"id": index, "event_id": index, "embedding_enc": _sealed_vector(cipher, index, [1.0, 0.0])}
        for index in (3, 2)
    ]
    pool = RecordingPool([rows, rows])
    vectors = VectorMemory(pool, cipher, _phantom_embedder())

    await vectors.load_cache(batch=2, max_chunks=2)

    assert len(pool.queries) == 1
    assert vectors._cache_ids == [2, 3]


async def test_warm_recall_ranks_in_memory_and_never_rescans() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    rows = [
        {"id": 2, "event_id": 9, "embedding_enc": _sealed_vector(cipher, 2, [1.0, 0.0])},
        {"id": 1, "event_id": 8, "embedding_enc": _sealed_vector(cipher, 1, [0.0, 1.0])},
    ]
    detail = [
        {
            "id": 2,
            "text_enc": cipher.encrypt_text("the invoice is due", aad="chunks:text_enc:2"),
            "meta_enc": cipher.encrypt_json(
                {"source": "gmail", "kind": "mail", "location": ""},
                aad="memory_chunks:meta_enc:2",
            ),
        }
    ]
    pool = RecordingPool([rows, detail, detail])
    vectors = VectorMemory(pool, cipher, _phantom_embedder())

    first = await vectors.search_vector([1.0, 0.0], limit=1, min_similarity=0.0)
    after_cold = len(pool.queries)
    second = await vectors.search_vector([1.0, 0.0], limit=1, min_similarity=0.0)

    assert first[0].text == "the invoice is due" and second[0].text == "the invoice is due"
    warm_queries = pool.queries[after_cold:]
    assert len(warm_queries) == 1
    assert "= ANY" in warm_queries[0]
    assert "ORDER BY id DESC" not in warm_queries[0]


async def test_a_freshly_indexed_chunk_joins_the_matrix_without_a_scan() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    rows = [
        {"id": 1, "event_id": 8, "embedding_enc": _sealed_vector(cipher, 1, [0.0, 1.0])},
    ]
    detail = [
        {
            "id": 2,
            "text_enc": cipher.encrypt_text("the new page", aad="chunks:text_enc:2"),
            "meta_enc": cipher.encrypt_json(
                {"source": "drive", "kind": "doc", "location": "page 2"},
                aad="memory_chunks:meta_enc:2",
            ),
        }
    ]
    pool = RecordingPool([rows, detail])
    vectors = VectorMemory(pool, cipher, _phantom_embedder())
    await vectors.load_cache(batch=10, max_chunks=10)

    # the chunk writer's notification is the only path a new row needs
    vectors._notify(
        {
            "action": "add",
            "chunk_id": 2,
            "event_id": 9,
            "vector": [1.0, 0.0],
            "text": "the new page",
        }
    )
    hits = await vectors.search_vector([1.0, 0.0], limit=1, min_similarity=0.0)

    assert [hit.text for hit in hits] == ["the new page"]
    assert len(pool.queries) == 2  # one load, one top-k detail read


async def test_trace_summaries_never_select_the_payload() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    pool = RecordingPool(
        [
            [
                {
                    "id": 1,
                    "kind": "turn",
                    "label_enc": cipher.encrypt_text(
                        "a quiet turn", aad="decision_traces:label_enc:1"
                    ),
                    "created_at": datetime.now(UTC),
                }
            ]
        ]
    )
    traces = Traces(pool, cipher)
    rows = await traces.recent_summaries(limit=10)

    assert [row.label for row in rows] == ["a quiet turn"]
    assert rows[0].payload == {}
    assert "trace_enc" not in pool.queries[0]
    assert "label_enc" in pool.queries[0]


async def test_scheduler_claims_ask_a_datetime_until_something_is_due() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    pool = RecordingPool()
    scheduler = Scheduler(pool, cipher, FakeAudit())

    # boot teaches the schedule with one read; every later claim is free
    assert await scheduler.overdue_pending() == []
    after_boot = pool.calls
    for _ in range(100):
        assert await scheduler.claim_due() == []
    assert pool.calls == after_boot


def test_panel_source_has_no_interval_pollers() -> None:
    source = (REPO_ROOT / "webapp/src/store.jsx").read_text(encoding="utf-8")
    assert "setInterval" not in source
    # and the refresh path exists instead
    assert "refreshAll" in source


def test_the_backfill_retry_is_not_a_minute_by_minute_poll() -> None:
    from aether.agent.loop import BACKFILL_SECONDS

    assert BACKFILL_SECONDS >= 300


async def test_trace_dataclass_still_carries_a_full_payload_for_detail() -> None:
    # the summary path is a read optimization only; the detail route still
    # reads the sealed payload
    trace = Trace(id=1, kind="turn", label="x", payload={"a": 1}, created_at=datetime.now(UTC))
    assert trace.payload == {"a": 1}


@pytest.mark.parametrize("value,expected", [(None, True), ("", True), (" hi ", False), ([], True)])
def test_poll_emptiness_detection(value, expected) -> None:
    from aether.agent.loop import _poll_empty

    assert _poll_empty(value) is expected


def test_wrapped_empty_poll_envelopes_read_as_empty() -> None:
    from aether.agent.loop import _poll_empty

    assert _poll_empty({"successful": True, "data": {"messages": []}}) is True
    assert _poll_empty({"successful": True, "data": {"messages": [{"id": 1}]}}) is False
