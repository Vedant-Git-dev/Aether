"""Keys pasted in chat, kept encrypted — the .env-free half of /apps.

A key pasted in an /apps answer is consumed before ingest (the walk
answers in the deterministic command path — no model, no memory, no
trace), never echoed back (replies name the NAME only), and lands here
with the same AES-256-GCM as all other content, bound to its row through
the cipher's AAD. The resolver reads this store before .env, so a paste
is the most recent deliberate act and wins over an older line — and .env
keeps working for every name that never gets pasted.

The trade-off is stated plainly: the value transits the chat platform
and sits in its history until the user deletes the message. That is the
price of never editing a file on a server; docs advise deleting the sent
message right after the "stored." reply.
"""

from __future__ import annotations

from typing import Any

from .memory.crypto import Cipher
from .memory.rows import CipherRows, Rows, ScopedRows
from .vault.scope import KeyScope


class SecretStore:
    """App keys in Postgres, encrypted at rest — the OAuthTokenStore shape
    (one-step upsert, name-bound AAD), name-keyed."""

    def __init__(self, pool: Any, cipher: Cipher | None, *, rows: Rows | None = None) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("SecretStore needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool

    @classmethod
    def scoped(cls, pool: Any, scope: KeyScope, *, ver: int = 1) -> SecretStore:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, None, rows=ScopedRows(scope, cat="credentials", ver=ver))

    async def set(self, name: str, value: str) -> None:
        self._rows.preflight()
        await self._pool.execute(
            "INSERT INTO app_secrets (name, value_enc, updated_at)"
            " VALUES ($1, $2, now())"
            " ON CONFLICT (name) DO UPDATE SET"
            " value_enc = EXCLUDED.value_enc, updated_at = now()",
            name,
            self._rows.seal_bytes(
                value.encode("utf-8"),
                table="app_secrets",
                column="value_enc",
                row_id=name,
                aad=f"app_secrets:value:{name}",
            ),
        )

    async def get(self, name: str) -> str | None:
        self._rows.preflight()
        row = await self._pool.fetchrow("SELECT value_enc FROM app_secrets WHERE name = $1", name)
        if row is None:
            return None
        return self._rows.open_bytes(
            bytes(row["value_enc"]),
            table="app_secrets",
            column="value_enc",
            row_id=name,
            aad=f"app_secrets:value:{name}",
        ).decode("utf-8")

    async def names(self) -> set[str]:
        """Which key names exist right now — names only, never values."""
        self._rows.preflight()
        rows = await self._pool.fetch("SELECT name FROM app_secrets")
        return {row["name"] for row in rows}
