"""Persisted, user-editable agent behavior settings.

Currently just the personality/instructions block the owner can write in
the web UI, which gets appended to the real system prompt on every turn —
not a cosmetic setting, it changes what the model actually sees.
"""

from __future__ import annotations

import asyncpg

from ..memory.crypto import Cipher

MAX_PERSONALITY_CHARS = 4000
_AAD = "agent_settings:personality_enc:1"


class AgentSettings:
    """Single-row store: one owner, one personality block."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def get_personality(self) -> str:
        row = await self._pool.fetchrow("SELECT personality_enc FROM agent_settings WHERE id = 1")
        if row is None or row["personality_enc"] is None:
            return ""
        try:
            data = self._cipher.decrypt_json(row["personality_enc"], aad=_AAD)
        except Exception:
            return ""
        return str(data.get("text", "")) if isinstance(data, dict) else ""

    async def set_personality(self, text: str) -> str:
        text = text.strip()[:MAX_PERSONALITY_CHARS]
        blob = self._cipher.encrypt_json({"text": text}, aad=_AAD)
        await self._pool.execute(
            "INSERT INTO agent_settings (id, personality_enc, updated_at)"
            " VALUES (1, $1, now())"
            " ON CONFLICT (id) DO UPDATE SET personality_enc = $1, updated_at = now()",
            blob,
        )
        return text
