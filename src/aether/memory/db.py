"""asyncpg pool + a tiny forward-only migration runner."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg

from ..config_validation import normalize_database_url
from ..db_meter import MeteredPool, QueryMeter
from ..db_resilience import CircuitBreaker

log = logging.getLogger("aether.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
ROLLBACK_DIR = MIGRATIONS_DIR / "rollback"

Before = Callable[[str], Awaitable[None]]

# the one shared pool: a zero floor and a short idle lifetime release every
# connection when quiet, so a client cannot keep the database awake on its own
POOL_MIN_SIZE = 0
POOL_MAX_SIZE = 5
POOL_IDLE_SECONDS = 60.0

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def create_pool(url: str, *, breaker: CircuitBreaker | None = None) -> MeteredPool:
    """One measured pool. Every store receives this wrapper and calls it like
    an asyncpg pool; each query lands in the process-local query meter."""
    pool = await asyncpg.create_pool(
        normalize_database_url(url),
        min_size=max(0, min(POOL_MIN_SIZE, POOL_MAX_SIZE)),
        max_size=max(1, POOL_MAX_SIZE),
        max_inactive_connection_lifetime=max(1.0, POOL_IDLE_SECONDS),
        command_timeout=30,
    )
    return MeteredPool(pool, QueryMeter(), breaker=breaker)


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


async def rollback_migration(pool: asyncpg.Pool, version: str) -> bool:
    """Undo one applied migration from `migrations/rollback/<version>.sql`.

    The forward runner never reads this directory — its globs are not
    recursive — so a down script can sit beside the schema it undoes without
    ever being applied by accident. The version row is removed by the script
    itself, after the schema is restored.
    """
    path = ROLLBACK_DIR / f"{version}.sql"
    if not path.is_file():
        raise FileNotFoundError(f"no rollback script for {version}")
    async with pool.acquire() as conn:
        applied = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = $1", version)
    if applied is None:
        return False
    sql = path.read_text(encoding="utf-8")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(sql)
    log.info("rolled back migration %s", version)
    return True
