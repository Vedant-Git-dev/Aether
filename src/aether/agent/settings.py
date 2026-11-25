"""Persisted, user-editable agent behavior settings.

Currently just the personality/instructions block the owner can write in
the web UI, which gets appended to the real system prompt on every turn —
not a cosmetic setting, it changes what the model actually sees.
"""

from __future__ import annotations

import asyncpg

from ..memory.crypto import Cipher
from ..memory.rows import CipherRows, Rows, ScopedRows
from ..vault.scope import KeyScope

MAX_PERSONALITY_CHARS = 4000
_AAD = "agent_settings:personality_enc:1"


class AgentSettings:
    """Single-row store: one owner, one personality block."""

    def __init__(
        self, pool: asyncpg.Pool, cipher: Cipher | None, *, rows: Rows | None = None
    ) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("AgentSettings needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool

    @classmethod
    def scoped(cls, pool: asyncpg.Pool, scope: KeyScope, *, ver: int = 1) -> AgentSettings:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, None, rows=ScopedRows(scope, cat="operations", ver=ver))

    async def get_personality(self) -> str:
        self._rows.preflight()
        row = await self._pool.fetchrow("SELECT personality_enc FROM agent_settings WHERE id = 1")
        if row is None or row["personality_enc"] is None:
            return ""
        try:
            data = self._rows.open(
                row["personality_enc"],
                table="agent_settings",
                column="personality_enc",
                row_id=1,
                aad=_AAD,
            )
        except Exception:
            if self._scoped:
                raise
            return ""
        return str(data.get("text", "")) if isinstance(data, dict) else ""

    async def set_personality(self, text: str) -> str:
        self._rows.preflight()
        text = text.strip()[:MAX_PERSONALITY_CHARS]
        blob = self._rows.seal(
            {"text": text},
            table="agent_settings",
            column="personality_enc",
            row_id=1,
            aad=_AAD,
        )
        await self._pool.execute(
            "INSERT INTO agent_settings (id, personality_enc, updated_at)"
            " VALUES (1, $1, now())"
            " ON CONFLICT (id) DO UPDATE SET personality_enc = $1, updated_at = now()",
            blob,
        )
        return text
