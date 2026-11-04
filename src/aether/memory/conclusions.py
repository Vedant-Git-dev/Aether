"""The conclusions cache: answers about live state, with a shelf life.

Plan §3 and §5. A conclusion is what Aether decided a moment ago about
something that changes — "nothing urgent", "the inbox is clear", "the build
is green". It is keyed by a blind token over the question's shape, and its
value is sealed with the time it was true (`as_of`) and the time it stops
being usable (`valid_until`).

`get_fresh` is the whole point: a state-of-now question must never be
answered from a stale conclusion, so a value past its `valid_until` reads as
absent and the caller re-queries the live source. Mailbox state gets the
`memory.conclusion_ttl_seconds` window (about two minutes); anything with a
longer natural life passes its own `ttl_seconds`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .crypto import Cipher

log = logging.getLogger("aether.memory.conclusions")

DEFAULT_TTL_SECONDS = 120


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Conclusion:
    key: str
    value: Any
    as_of: datetime
    valid_until: datetime
    source: str

    def is_fresh(self, now: datetime) -> bool:
        return now < self.valid_until

    def as_of_text(self) -> str:
        return f"as of {self.as_of:%H:%M}"


class Conclusions:
    """The sealed, time-bounded cache of live-state answers."""

    def __init__(
        self, pool: asyncpg.Pool, cipher: Cipher, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._ttl_seconds = max(1, int(ttl_seconds))

    def _token(self, key: str) -> bytes:
        return self._cipher.blind_token("conclusion", key or "")

    def _aad(self, token: bytes) -> str:
        return f"conclusions:value_enc:{token.hex()}"

    async def set(
        self,
        key: str,
        value: Any,
        *,
        source: str = "",
        as_of: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> Conclusion:
        """Record one answer about now, valid for its window."""
        now = as_of or datetime.now(UTC)
        ttl = self._ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        valid_until = now + timedelta(seconds=ttl)
        token = self._token(key)
        body = {
            "value": value,
            "as_of": now.isoformat(),
            "valid_until": valid_until.isoformat(),
            "source": source,
        }
        await self._pool.execute(
            "INSERT INTO conclusions (key_token, value_enc, key_version, created_at)"
            " VALUES ($1, $2, $3, now())"
            " ON CONFLICT (key_token) DO UPDATE SET"
            " value_enc = EXCLUDED.value_enc, key_version = EXCLUDED.key_version,"
            " created_at = now()",
            token,
            self._cipher.encrypt_json(body, aad=self._aad(token)),
            self._cipher.key_id,
        )
        return Conclusion(key=key, value=value, as_of=now, valid_until=valid_until, source=source)

    async def get(self, key: str) -> Conclusion | None:
        """The stored conclusion whatever its age, or None when unreadable."""
        token = self._token(key)
        row = await self._pool.fetchrow(
            "SELECT value_enc FROM conclusions WHERE key_token = $1", token
        )
        if row is None:
            return None
        try:
            body = self._cipher.decrypt_json(row["value_enc"], aad=self._aad(token))
        except Exception:
            log.warning("conclusion undecryptable — treated as absent")
            return None
        if not isinstance(body, dict):
            return None
        as_of = _optional_time(body.get("as_of")) or datetime.now(UTC)
        valid_until = _optional_time(body.get("valid_until")) or as_of
        return Conclusion(
            key=key,
            value=body.get("value"),
            as_of=as_of,
            valid_until=valid_until,
            source=str(body.get("source") or ""),
        )

    async def get_fresh(self, key: str, now: datetime | None = None) -> Conclusion | None:
        """The conclusion only while it is still true.

        An expired value is not served — the caller must ask the live source
        again rather than repeat a state that has moved on (§5 freshness).
        """
        now = now or datetime.now(UTC)
        found = await self.get(key)
        if found is None or not found.is_fresh(now):
            return None
        return found

    async def forget(self, key: str) -> None:
        await self._pool.execute("DELETE FROM conclusions WHERE key_token = $1", self._token(key))
