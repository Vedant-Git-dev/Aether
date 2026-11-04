"""The cutover: plaintext columns are gone, the sealer still works on the
legacy shape when one is present, and re-running is a no-op."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.db import run_migrations
from aether.memory.seal_migrate import (
    LEGACY_COLUMNS,
    backfill,
    legacy_columns_present,
    pending_counts,
)
from aether.memory.storage import schema_violations


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


@pytest.mark.integration
async def test_migrations_are_rerunnable(db) -> None:
    assert await run_migrations(db) == []


@pytest.mark.integration
async def test_the_cutover_dropped_every_legacy_column(db) -> None:
    columns = await db.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public'"
    )
    present = {f"{row['table_name']}.{row['column_name']}" for row in columns} & {
        f"{table}.{column}" for table, cols in LEGACY_COLUMNS.items() for column in cols
    }
    assert present == set()
    assert await schema_violations(db) == []
    assert await legacy_columns_present(db) is False


@pytest.mark.integration
async def test_the_cli_sees_an_applied_cutover(db) -> None:
    # the sealer's own check, so `--apply` on a cut-over database says so
    # instead of querying a column that no longer exists
    assert await legacy_columns_present(db) is False
    assert all(count == 0 for count in (await pending_counts(db, tables={"events"})).values())


@pytest.mark.integration
async def test_the_sealer_still_fills_a_legacy_approval(db) -> None:
    cipher = _cipher()
    await db.execute(
        "ALTER TABLE pending_approvals ADD COLUMN rules_matched TEXT NOT NULL DEFAULT ''"
    )
    await db.execute("ALTER TABLE pending_approvals ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    try:
        approval_id = await db.fetchval(
            "INSERT INTO pending_approvals"
            " (tool_name, params_enc, status, expires_at, rules_matched, note)"
            " VALUES ('gmail__send', $1, 'pending', $2, 'user rule 1', 'replying')"
            " RETURNING id",
            b"x",
            datetime.now(UTC) + timedelta(hours=1),
        )
        results = await backfill(db, cipher, batch=10, sleep=0, tables={"pending_approvals"})
        assert [result.sealed for result in results] == [1]
        assert results[0].remaining == 0
        row = await db.fetchrow(
            "SELECT rules_matched_enc, note_enc FROM pending_approvals WHERE id = $1",
            approval_id,
        )
        assert (
            cipher.decrypt_text(
                row["rules_matched_enc"],
                aad=f"pending_approvals:rules_matched_enc:{approval_id}",
            )
            == "user rule 1"
        )
        assert (
            cipher.decrypt_text(row["note_enc"], aad=f"pending_approvals:note_enc:{approval_id}")
            == "replying"
        )
    finally:
        await db.execute("ALTER TABLE pending_approvals DROP COLUMN IF EXISTS rules_matched")
        await db.execute("ALTER TABLE pending_approvals DROP COLUMN IF EXISTS note")


@pytest.mark.integration
async def test_an_unreadable_event_payload_is_left_alone(db) -> None:
    cipher = _cipher()
    await db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    await db.execute("ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    await db.execute("ALTER TABLE events ADD COLUMN occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    await db.execute("ALTER TABLE events ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
    await db.execute("ALTER TABLE events ADD COLUMN salience_score REAL NOT NULL DEFAULT 0")
    await db.execute("ALTER TABLE events ADD COLUMN memorable BOOLEAN NOT NULL DEFAULT false")
    await db.execute("ALTER TABLE events ADD COLUMN meta JSONB NOT NULL DEFAULT '{}'::jsonb")
    try:
        await db.execute(
            "INSERT INTO events (source, kind, occurred_at, content_hash, payload_enc)"
            " VALUES ('gmail', 'message', now(), 'broken-hash', $1)",
            b"not a sealed payload",
        )
        results = await backfill(db, cipher, batch=50, sleep=0, tables={"events"})
        assert results[0].failed == 1
        assert results[0].remaining == 1
        row = await db.fetchrow("SELECT meta_enc FROM events WHERE content_hash = 'broken-hash'")
        assert row["meta_enc"] is None
    finally:
        for column in (
            "source",
            "kind",
            "occurred_at",
            "content_hash",
            "salience_score",
            "memorable",
            "meta",
        ):
            await db.execute(f"ALTER TABLE events DROP COLUMN IF EXISTS {column}")


@pytest.mark.integration
async def test_pending_counts_read_zero_after_the_cutover(db) -> None:
    # the tables are empty here; the point is that every job's count query
    # still runs against the sealed schema without referencing a gone column
    counts = await pending_counts(db)
    assert counts and all(count == 0 for count in counts.values())


@pytest.mark.integration
async def test_a_pending_cutover_runs_the_hook_before_the_drop(db) -> None:
    """The deploy path: `before` sees the cutover version first, so the boot
    can seal legacy rows in the same boot that drops their columns."""
    await db.execute("DELETE FROM schema_migrations WHERE version = '018_sealed_cutover'")
    seen: list[str] = []

    async def before(version: str) -> None:
        seen.append(version)

    applied = await run_migrations(db, before=before)
    assert applied == ["018_sealed_cutover"]
    assert seen == ["018_sealed_cutover"]
