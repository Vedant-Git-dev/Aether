"""asyncpg pool + a tiny forward-only migration runner."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg

from ..config_validation import normalize_database_url

log = logging.getLogger("aether.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

Before = Callable[[str], Awaitable[None]]

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def create_pool(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        normalize_database_url(url), min_size=1, max_size=5, command_timeout=30
    )


async def run_migrations(pool: asyncpg.Pool, *, before: Before | None = None) -> list[str]:
    """Apply pending migrations/*.sql in filename order; return applied versions.

    `before` runs with each pending version before that migration's SQL, so
    the boot can seal legacy rows between the expand (`017`) and the cutover
    (`018`) instead of requiring a separate deploy step."""
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_MIGRATIONS_TABLE)
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        if before is not None:
            await before(version)
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                version,
            )
        log.info("applied migration %s", version)
        applied.append(version)
    return applied
