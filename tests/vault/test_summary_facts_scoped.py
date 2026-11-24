"""Scoped summaries and facts: AE2 bodies and keyed lookups under a task
scope, tested against scratch Postgres with the tests-only fake vault.

The legacy serving path stays intact. These tests exercise explicit scoped
construction, the preserved token inputs, the platform/period/fact domains,
fail-closed reads and zero-SQL denials.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fake_vault import FakeVault

from aether.memory.facts import Facts
from aether.memory.summaries import DAILY, Summaries
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import VaultClosed, VaultLocked

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


class _NoSqlPool:
    """Any query is a test failure: a denied read must issue zero SQL."""

    async def fetchrow(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetch(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetchval(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def execute(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    def acquire(self) -> None:
        raise AssertionError("a denied read issued SQL")


async def test_scoped_summary_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        summaries = Summaries.scoped(db, scope)
        written = await summaries.upsert(
            DAILY, "2026-11-03", "the day in one line", [1, 2], now=NOW
        )
        assert written is not None
        row = await db.fetchrow(
            "SELECT body_enc, period_token, key_version FROM summaries WHERE id = $1",
            written.id,
        )
        assert row["body_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        assert row["period_token"] == scope.token("summary.period", f"{DAILY}\x1f2026-11-03")
        found = await summaries.get(DAILY, "2026-11-03")
        assert found is not None
        assert found.text == "the day in one line"
        assert found.source_event_ids == (1, 2)
        # an upsert replaces in place, through the same scoped token
        again = await summaries.upsert(DAILY, "2026-11-03", "rewritten", now=NOW)
        assert again is not None and again.id == written.id
        assert (await summaries.get(DAILY, "2026-11-03")).text == "rewritten"
        assert [item.id for item in await summaries.recent(DAILY)] == [written.id]


async def test_scoped_summary_denied_reads_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", [("workspace", 1)]) as scope:
        summaries = Summaries.scoped(pool, scope)
        with pytest.raises(VaultLocked):
            await summaries.get(DAILY, "2026-11-03")
        with pytest.raises(VaultLocked):
            await summaries.recent(DAILY)
        with pytest.raises(VaultLocked):
            await summaries.upsert(DAILY, "2026-11-03", "x", now=NOW)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        summaries = Summaries.scoped(pool, scope)
    with pytest.raises(VaultClosed):
        await summaries.get(DAILY, "2026-11-03")


async def test_scoped_summary_corrupt_body_raises(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        summaries = Summaries.scoped(db, scope)
        written = await summaries.upsert(DAILY, "2026-11-03", "text", now=NOW)
        assert written is not None
        blob = await db.fetchval("SELECT body_enc FROM summaries WHERE id = $1", written.id)
        tampered = bytearray(blob)
        tampered[-1] ^= 0xFF
        await db.execute(
            "UPDATE summaries SET body_enc = $1 WHERE id = $2", bytes(tampered), written.id
        )
        with pytest.raises(VaultProtocolError):
            await summaries.get(DAILY, "2026-11-03")


async def test_scoped_fact_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        facts = Facts.scoped(db, scope)
        result = await facts.consolidate(
            subject_identity_id=None,
            subject="User",
            key="timezone",
            value="Asia/Kolkata",
            provenance=[7],
            now=NOW,
        )
        assert result.stored and result.fact is not None
        row = await db.fetchrow(
            "SELECT body_enc, key_token, key_version FROM facts WHERE id = $1", result.fact.id
        )
        assert row["body_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        # the exact legacy input is preserved; only the deriving key changes
        assert row["key_token"] == scope.token("fact.subject", "0\x1fuser\x1ftimezone")
        found = await facts.get(result.fact.id)
        assert found is not None
        assert found.value == "Asia/Kolkata"
        assert found.provenance == (7,)
        active = await facts.active(now=NOW)
        assert [fact.id for fact in active] == [result.fact.id]


async def test_scoped_fact_denied_reads_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", [("workspace", 1)]) as scope:
        facts = Facts.scoped(pool, scope)
        with pytest.raises(VaultLocked):
            await facts.get(1)
        with pytest.raises(VaultLocked):
            await facts.active()
        with pytest.raises(VaultLocked):
            await facts.consolidate(
                subject_identity_id=None,
                subject="User",
                key="timezone",
                value="Asia/Kolkata",
                provenance=[7],
            )
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        facts = Facts.scoped(pool, scope)
    with pytest.raises(VaultClosed):
        await facts.get(1)


async def test_scoped_fact_corrupt_body_raises(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        facts = Facts.scoped(db, scope)
        result = await facts.consolidate(
            subject_identity_id=None,
            subject="User",
            key="timezone",
            value="Asia/Kolkata",
            provenance=[7],
            now=NOW,
        )
        assert result.fact is not None
        blob = await db.fetchval("SELECT body_enc FROM facts WHERE id = $1", result.fact.id)
        tampered = bytearray(blob)
        tampered[-1] ^= 0xFF
        await db.execute(
            "UPDATE facts SET body_enc = $1 WHERE id = $2", bytes(tampered), result.fact.id
        )
        with pytest.raises(VaultProtocolError):
            await facts.get(result.fact.id)
