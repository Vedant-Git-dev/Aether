"""ProcessedUpdates against a real Postgres — the once-only word needs the
real ON CONFLICT to be trusted. Integration (scratch-DB) tests: they run
only when AETHER_TEST_DATABASE_URL is set.

The unit double (FakeProcessedLedger in test_messaging_units.py) pins the
connector's side of the contract; this file pins the ledger's own: one
winner per update, sources are separate ledgers, and the window forgets.
"""

from __future__ import annotations

from typing import Any

import pytest

from aether.connectors.processed import ProcessedUpdates

pytestmark = pytest.mark.integration


async def test_the_first_sighting_wins_and_the_duplicate_loses(db) -> None:
    ledger = ProcessedUpdates(db)
    assert await ledger.record("telegram", 10) is True
    assert await ledger.record("telegram", 10) is False  # the redelivery
    # each source keeps its own ledger — the same numeric id on another
    # platform is a different update
    assert await ledger.record("discord", 10) is True


async def test_rows_older_than_the_window_are_forgotten(db) -> None:
    ledger = ProcessedUpdates(db)
    assert await ledger.record("telegram", 11) is True
    # age the row past the window: the prune runs before the insert, so
    # the same update reads as unseen — no duplicate can still arrive a
    # month later, but the table can never grow without bound
    await db.execute("UPDATE processed_updates SET created_at = now() - interval '31 days'")
    assert await ledger.record("telegram", 11) is True


class _PruneDownPool:
    """Wraps the real pool: the connection it hands out fails every DELETE
    (the prune) and passes everything else through — the shape of a ledger
    that can still record when the broom is broken."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def acquire(self) -> Any:
        return _Wrap(self._pool.acquire())


class _Wrap:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def __aenter__(self) -> Any:
        conn = await self._inner.__aenter__()

        class Conn:
            async def execute(self, query: str, *args: Any) -> Any:
                if "DELETE" in query:
                    raise RuntimeError("prune down")
                return await conn.execute(query, *args)

            def __getattr__(self, name: str) -> Any:
                return getattr(conn, name)

        return Conn()

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)


async def test_a_prune_failure_never_costs_a_sighting(db) -> None:
    """The prune is best-effort by contract — even when it cannot run, the
    sighting still records and the answer still comes back."""
    ledger = ProcessedUpdates(_PruneDownPool(db))
    assert await ledger.record("telegram", 12) is True
    assert await ledger.record("telegram", 12) is False
