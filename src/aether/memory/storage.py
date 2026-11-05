"""The storage guard's async side: the boot audit and the sealed write path.

The registry itself lives in `memory/allowlist.py` so the CI guard can run
without database imports. This module re-exports it and adds the two
database-facing pieces:

  - `enforce_schema` checks `information_schema.columns` at boot
  - `SealedStore` is the narrow writer for rows with sealed columns

`LEGACY_PLAINTEXT` is the migration ledger; it shrinks to empty as the
plaintext columns are sealed and dropped. Until then the audit warns, and
`enforce` mode refuses to boot.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import asyncpg

from .allowlist import (
    IDENTIFIER_RE,
    KEY_VERSION,
    LEGACY_PLAINTEXT,
    PLAINTEXT_ALLOWLIST,
    SEALED_COLUMNS,
    StorageGuardError,
    audit_columns,
    check_identifier,
    check_write,
    classify,
)
from .crypto import Cipher

log = logging.getLogger("aether.memory.storage")

__all__ = [
    "IDENTIFIER_RE",
    "KEY_VERSION",
    "LEGACY_PLAINTEXT",
    "PLAINTEXT_ALLOWLIST",
    "SEALED_COLUMNS",
    "SealedStore",
    "StorageGuardError",
    "audit_columns",
    "check_identifier",
    "check_write",
    "classify",
    "enforce_schema",
    "schema_violations",
]


async def schema_violations(pool: asyncpg.Pool) -> list[str]:
    rows = await pool.fetch(
        "SELECT table_name, column_name, data_type"
        " FROM information_schema.columns"
        " WHERE table_schema = 'public'"
        " ORDER BY table_name, ordinal_position"
    )
    return audit_columns(rows)


async def enforce_schema(pool: asyncpg.Pool, mode: str = "warn") -> list[str]:
    """Check the live schema. `off` skips; `warn` logs and boots; `enforce`
    refuses to start. Returns the violations for tests and logs."""
    if mode == "off":
        return []
    violations = await schema_violations(pool)
    if not violations:
        log.info("schema audit: every column is allowlisted or sealed")
        return []
    shown = violations[:8]
    more = len(violations) - len(shown)
    detail = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
    if mode == "enforce":
        raise StorageGuardError(f"schema audit failed ({len(violations)} violation(s)): {detail}")
    log.warning(
        "schema audit: %d column(s) still violate the sealed-storage rule — %s",
        len(violations),
        detail,
    )
    return violations


class SealedStore:
    """The only sanctioned writer for sealed rows.

    It validates every table and column against the registry, inserts the
    row to obtain its id (the AAD binds to that id), then seals the values
    and updates the same row. Reads that need plaintext columns still go
    through the owning store; this class exists so no new code can write a
    user-derived value into a plain column by accident.
    """

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher
        self._columns: dict[str, frozenset[str]] = {}

    async def _existing_columns(self, table: str) -> frozenset[str]:
        """The table's real columns, so the write path can stamp a key
        version only where the migration has added the column. One query
        per table per process."""
        cached = self._columns.get(table)
        if cached is None:
            rows = await self._pool.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = $1",
                table,
            )
            cached = frozenset(str(row["column_name"]) for row in rows)
            self._columns[table] = cached
        return cached

    async def insert(
        self,
        table: str,
        *,
        plaintext: Mapping[str, Any] | None = None,
        sealed: Mapping[str, Any] | None = None,
        returning: str = "id",
    ) -> asyncpg.Record:
        plain = dict(plaintext or {})
        if KEY_VERSION in await self._existing_columns(table):
            plain.setdefault(KEY_VERSION, self._cipher.key_id)
        check_write(table, plain)
        for column in sealed or {}:
            if classify(table, column) != "sealed":
                raise StorageGuardError(f"{table}.{column} is not a sealed column")
        check_identifier(returning)

        columns = [*plain, *(sealed or {})]
        if not columns:
            raise StorageGuardError("insert needs at least one column")
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        values = [plain.get(c, b"") for c in columns]
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)})"
            f" VALUES ({placeholders}) RETURNING {returning}"
        )
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(sql, *values)
            if row is None:
                raise StorageGuardError(f"insert into {table} returned no row")
            if sealed:
                assignments: list[str] = []
                sealed_values: list[Any] = []
                for column, value in sealed.items():
                    sealed_values.append(
                        self.seal(value, table=table, column=column, row_id=row[returning])
                    )
                    assignments.append(f"{column} = ${len(sealed_values)}")
                sealed_values.append(row[returning])
                await conn.execute(
                    f"UPDATE {table} SET {', '.join(assignments)}"
                    f" WHERE {returning} = ${len(sealed_values)}",
                    *sealed_values,
                )
        return row

    async def update(
        self,
        table: str,
        row_id: Any,
        *,
        plaintext: Mapping[str, Any] | None = None,
        sealed: Mapping[str, Any] | None = None,
        key: str = "id",
    ) -> None:
        check_identifier(key)
        plain = dict(plaintext or {})
        check_write(table, plain)
        for column in sealed or {}:
            if classify(table, column) != "sealed":
                raise StorageGuardError(f"{table}.{column} is not a sealed column")
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in plain.items():
            values.append(value)
            assignments.append(f"{column} = ${len(values)}")
        for column, value in (sealed or {}).items():
            values.append(self.seal(value, table=table, column=column, row_id=row_id))
            assignments.append(f"{column} = ${len(values)}")
        if not assignments:
            return
        values.append(row_id)
        await self._pool.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE {key} = ${len(values)}",
            *values,
        )

    def seal(self, value: Any, *, table: str, column: str, row_id: Any) -> bytes:
        """Seal one value under the row's own AAD."""
        return self._cipher.encrypt_json(value, aad=f"{table}:{column}:{row_id}")

    def unseal(self, blob: bytes, *, table: str, column: str, row_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=f"{table}:{column}:{row_id}")


# ---------------------------------------------------------------------------
# the chunk rows the in-memory retrieval indexes are built from
# ---------------------------------------------------------------------------
# Chunk text is sealed, so the BM25 index decrypts it in process memory. The
# SQL stays here for the same reason the workspace's does: one module owns it.


async def newest_chunk_rows(pool: asyncpg.Pool, limit: int) -> list[asyncpg.Record]:
    """The newest chunk rows by id — the boot rebuild's bounded read."""
    return await pool.fetch(
        "SELECT id, event_id, text_enc, meta_enc, ordinal, key_version"
        " FROM memory_chunks ORDER BY id DESC LIMIT $1",
        limit,
    )


async def older_chunk_rows(pool: asyncpg.Pool, before_id: int, limit: int) -> list[asyncpg.Record]:
    """One cold batch of chunks older than `before_id`, newest first."""
    return await pool.fetch(
        "SELECT id, event_id, text_enc, meta_enc, ordinal, key_version"
        " FROM memory_chunks WHERE id < $1 ORDER BY id DESC LIMIT $2",
        before_id,
        limit,
    )


# ---------------------------------------------------------------------------
# the workspace's sealed file rows
# ---------------------------------------------------------------------------
# The workspace owns the content logic; these four functions own the SQL, so
# the storage guard's "SQL lives in the storage API" rule holds.


async def workspace_row(pool: asyncpg.Pool, token: bytes) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT id, name_enc, content_enc FROM workspace_files WHERE name_token = $1",
        token,
    )


async def workspace_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT id, name_enc, content_enc FROM workspace_files")


async def upsert_workspace_file(
    pool: asyncpg.Pool, cipher: Cipher, *, token: bytes, name: str, content: str
) -> None:
    """Insert the row first so the AAD can bind to its id, then seal both
    the display name and the content."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT id FROM workspace_files WHERE name_token = $1", token)
        if row is None:
            row = await conn.fetchrow(
                "INSERT INTO workspace_files (name_token, name_enc, content_enc,"
                " key_version) VALUES ($1, $2, $3, $4) RETURNING id",
                token,
                b"",
                b"",
                cipher.key_id,
            )
        row_id = row["id"]
        await conn.execute(
            "UPDATE workspace_files SET name_enc = $1, content_enc = $2,"
            " key_version = $3, updated_at = now() WHERE id = $4",
            cipher.encrypt_text(name, aad=f"workspace_files:name_enc:{row_id}"),
            cipher.encrypt_text(content, aad=f"workspace_files:content_enc:{row_id}"),
            cipher.key_id,
            row_id,
        )


async def delete_workspace_file(pool: asyncpg.Pool, token: bytes) -> None:
    await pool.execute("DELETE FROM workspace_files WHERE name_token = $1", token)
