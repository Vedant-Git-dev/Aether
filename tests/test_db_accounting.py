"""The database accounting layer: counts, rows, bytes, call sites and report."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aether.api import router as api_router
from aether.config import Settings
from aether.db_accounting import DBAccounting, MeteredPool, estimate_bytes
from aether.db_replay import active_seconds
from aether.db_stats import render


class _FakeConnection:
    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> list[dict]:
        return [{"text": "hello", "count": 2}]

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> dict:
        return {"text": "hello"}

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> int:
        return 3

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> str:
        return "UPDATE 1"

    def transaction(self) -> Any:
        return _NullContext()


class _NullContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _AcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConnection()

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> list[dict]:
        return await self.conn.fetch(query, *args, **kwargs)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> dict:
        return await self.conn.fetchrow(query, *args, **kwargs)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> int:
        return await self.conn.fetchval(query, *args, **kwargs)

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> str:
        return await self.conn.execute(query, *args, **kwargs)

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self.conn)

    def get_size(self) -> int:
        return 1

    def get_idle_size(self) -> int:
        return 1

    def get_min_size(self) -> int:
        return 1

    def get_max_size(self) -> int:
        return 5


def test_estimate_bytes_counts_text_and_scalars() -> None:
    assert estimate_bytes(None) == 0
    assert estimate_bytes("hello") == 5
    assert estimate_bytes(b"hello") == 5
    assert estimate_bytes(7) == 8
    assert estimate_bytes({"a": "bc"}) == 3
    assert estimate_bytes([b"ab", "cd"]) == 4


async def test_pool_and_connection_calls_are_metered() -> None:
    accounting = DBAccounting()
    pool = MeteredPool(_FakePool(), accounting)
    rows = await pool.fetch("SELECT text, count FROM events WHERE id = $1", 1)
    row = await pool.fetchrow("SELECT text FROM events WHERE id = $1", 1)
    value = await pool.fetchval("SELECT count(*) FROM events")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE events SET meta_enc = $1 WHERE id = $2", b"x", 1)

    snapshot = accounting.snapshot()
    assert rows and row == {"text": "hello"} and value == 3
    assert snapshot["totals"]["calls"] == 4
    assert snapshot["totals"]["rows"] == 3
    assert snapshot["totals"]["bytes"] > 0
    assert snapshot["connections"]["acquires"] == 1
    assert snapshot["connections"]["peak"] == 1
    assert snapshot["tables"][0]["table"] == "events"
    assert any(
        site["site"].endswith("test_pool_and_connection_calls_are_metered")
        for site in snapshot["sites"]
    )


def test_recording_can_be_disabled() -> None:
    accounting = DBAccounting(enabled=False)
    accounting.record("SELECT 1", {"x": "y"}, 1, 0.001)
    assert accounting.snapshot()["totals"]["calls"] == 0


def test_timeline_and_active_time_model() -> None:
    accounting = DBAccounting(collect_timeline=True)
    accounting.record("SELECT 1", None, 0, 0.0, site="test", at=0.0)
    accounting.record("SELECT 1", None, 0, 0.0, site="test", at=100.0)
    accounting.record("SELECT 1", None, 0, 0.0, site="test", at=700.0)
    assert accounting.timeline() == [0.0, 100.0, 700.0]
    # 0-300, 100-400 merge to 0-400; 700-1000 is its own window
    assert active_seconds(accounting.timeline(), 0, 1000, 300) == 700
    accounting.reset()
    assert accounting.timeline() == []


def test_stats_route_returns_the_snapshot_and_is_token_guarded() -> None:
    app = FastAPI()
    app.include_router(api_router)
    app.state.settings = Settings.without_env_file(api_token="secret")
    accounting = DBAccounting()
    accounting.record("SELECT id FROM events", [{"id": 1}], 1, 0.001, site="test.reader")
    app.state.db_accounting = accounting
    client = TestClient(app)

    assert client.get("/api/db/stats").status_code == 401
    response = client.get("/api/db/stats", headers={"x-aether-token": "secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["totals"]["calls"] == 1
    assert payload["sites"][0]["site"] == "test.reader"


def test_stats_route_says_plainly_when_accounting_is_not_wired() -> None:
    app = FastAPI()
    app.include_router(api_router)
    app.state.settings = Settings.without_env_file(api_token="secret")
    client = TestClient(app)
    response = client.get("/api/db/stats", headers={"x-aether-token": "secret"})
    assert response.status_code == 503


def test_report_labels_estimates_and_names_top_readers() -> None:
    text = render(
        {
            "totals": {"calls": 12, "rows": 30, "bytes": 2048, "seconds": 0.2},
            "uptime_seconds": 60,
            "connections": {"acquires": 3, "peak": 2, "size": 1},
            "sites": [
                {
                    "site": "aether.memory.vectors.search_vector",
                    "calls": 12,
                    "rows": 30,
                    "bytes": 2048,
                    "seconds": 0.2,
                }
            ],
            "tables": [{"table": "memory_chunks", "calls": 12, "rows": 30, "bytes": 2048}],
            "hourly": [0] * 8 + [12] + [0] * 15,
        }
    )
    assert "estimated" in text
    assert "aether.memory.vectors.search_vector" in text
    assert "memory_chunks" in text
    assert "SELECT" not in text


def test_cli_without_token_is_a_plain_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    from aether.db_stats import main

    assert main([]) == 1
    assert "AETHER_TOKEN" in capsys.readouterr().err
