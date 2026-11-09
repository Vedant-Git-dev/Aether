"""Notification state: what has already been reported to the user.

The state is per event or action, not per message. A failure that was
already reported must not be reported again inside the configured window,
even if a retry produces a fresh event row for the same action. Keys are
opaque strings built by the caller (``event:12``, ``action:7``); the store
never interprets them.

Only a keyed blind token and a timestamp are plaintext. The key itself is
sealed in the row body, so the table cannot leak what the user was told.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .crypto import Cipher

log = logging.getLogger("aether.memory.notifications")


@dataclass(frozen=True)
class Report:
    key: str
    reported_at: datetime


class NotificationStates(Protocol):
    async def recently_reported(
        self, keys: tuple[str, ...], window: timedelta, *, now: datetime | None = None
    ) -> set[str]: ...

    async def mark_reported(self, keys: tuple[str, ...], *, at: datetime | None = None) -> None: ...


class MemoryNotificationStates:
    """The in-process store — the default for tests and a bare loop."""

    def __init__(self) -> None:
        self._rows: dict[str, datetime] = {}

    async def recently_reported(
        self, keys: tuple[str, ...], window: timedelta, *, now: datetime | None = None
    ) -> set[str]:
        moment = now or datetime.now(UTC)
        cutoff = moment - window
        return {key for key in keys if key in self._rows and self._rows[key] >= cutoff}

    async def mark_reported(self, keys: tuple[str, ...], *, at: datetime | None = None) -> None:
        moment = at or datetime.now(UTC)
        for key in keys:
            self._rows[key] = moment

    def clear(self) -> None:
        self._rows.clear()


_COLUMNS = "id, key_token, body_enc, reported_at"


def _aad(key_token: bytes) -> str:
    return f"notification_reports:body_enc:{key_token.hex()}"


class DbNotificationStates:
    """The durable sealed store. Equality is by blind token; the key text is
    ciphertext bound to that token."""

    def __init__(self, pool: Any, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def recently_reported(
        self, keys: tuple[str, ...], window: timedelta, *, now: datetime | None = None
    ) -> set[str]:
        if not keys:
            return set()
        moment = now or datetime.now(UTC)
        cutoff = moment - window
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM notification_reports WHERE reported_at >= $1",
            cutoff,
        )
        wanted = set(keys)
        found: set[str] = set()
        for row in rows:
            key = self._open(row)
            if key in wanted:
                found.add(key)
        return found

    async def mark_reported(self, keys: tuple[str, ...], *, at: datetime | None = None) -> None:
        if not keys:
            return
        moment = at or datetime.now(UTC)
        for key in keys:
            token = self._cipher.blind_token("notification-key", key)
            body = self._cipher.encrypt_json({"key": key}, aad=_aad(token))
            await self._pool.execute(
                "INSERT INTO notification_reports"
                " (key_token, body_enc, reported_at, key_version)"
                " VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (key_token) DO UPDATE SET"
                " body_enc = EXCLUDED.body_enc,"
                " reported_at = EXCLUDED.reported_at,"
                " key_version = EXCLUDED.key_version",
                token,
                body,
                moment,
                self._cipher.key_id,
            )

    def _open(self, row: Any) -> str:
        token = row.get("key_token")
        blob = row.get("body_enc")
        if not token or not blob:
            return ""
        try:
            data = self._cipher.decrypt_json(blob, aad=_aad(bytes(token)))
        except Exception:
            log.warning("notification report undecryptable — treated as unreported")
            return ""
        return str(data.get("key") or "") if isinstance(data, dict) else ""
