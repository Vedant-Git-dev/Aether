"""asyncpg pool + a tiny forward-only migration runner."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger("aether.db")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def create_pool(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=30)


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    """Apply pending migrations/*.sql in filename order; return applied versions."""
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_MIGRATIONS_TABLE)
        done = {
            r["version"]
            for r in await conn.fetch("SELECT version FROM schema_migrations")
        }
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                    version,
                )
        log.info("applied migration %s", version)
        applied.append(version)
    return applied
