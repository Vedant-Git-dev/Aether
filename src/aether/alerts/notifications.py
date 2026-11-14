"""One row per notification an alert produced — the deterministic queue.

A match never sends words by itself and never waits for an unprompted turn.
It enqueues a candidate here, sealed, with the structured event fields the
wording is rendered from. The engine then decides, in code, whether the
candidate is delivered now, held for quiet hours, batched into a digest, or
suppressed (already told, daily cap, alert removed) — and records the outcome
on the row. A failed send stays pending with a retry time, so a dead surface
is a fact the next pass can act on instead of a swallowed error.

Plaintext here is the status vocabulary, ids, the urgency level, attempt
counts and timestamps. The event snapshot — sender, subject, summary — is
sealed and bound to its row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from ..memory.crypto import Cipher
from .spec import AlertEvent, dedupe_key

log = logging.getLogger("aether.alerts")

QUEUED = "queued"
HELD = "held"
SENT = "sent"
SUPPRESSED = "suppressed"
FAILED = "failed"

_COLUMNS = (
    "id, alert_id, event_id, status, style, urgent, attempts, next_try_at, event_enc, created_at"
)


def _aad(column: str, notification_id: int) -> str:
    return f"alert_notifications:{column}:{notification_id}"


@dataclass
class Notification:
    id: int
    alert_id: int
    event_id: int
    status: str
    style: str
    urgent: int
    attempts: int
    next_try_at: datetime | None
    event: AlertEvent
    created_at: datetime


class AlertNotifications:
    """The sealed notification queue and told-state ledger."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def enqueue(self, event: AlertEvent, *, style: str, key: str = "") -> int | None:
        """Queue one candidate. A repeated match for the same alert and item
        collides on its keyed token and produces no second row."""
        key = key or dedupe_key(event.alert_id, event.event_id)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO alert_notifications (alert_id, event_id, status,"
                    " style, urgent, event_enc, key_token, key_version)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                    " ON CONFLICT (key_token) DO NOTHING"
                    " RETURNING id",
                    event.alert_id,
                    event.event_id or None,
                    QUEUED,
                    style,
                    event.urgency,
                    b"",
                    self._cipher.blind_token("alert_notification", key),
                    self._cipher.key_id,
                )
                if row is None:
                    return None
                notification_id = int(row["id"])
                await conn.execute(
                    "UPDATE alert_notifications SET event_enc = $1 WHERE id = $2",
                    self._cipher.encrypt_json(
                        event.to_payload(), aad=_aad("event_enc", notification_id)
                    ),
                    notification_id,
                )
        except Exception:
            log.warning("could not queue an alert notification — continuing", exc_info=True)
            return None
        return notification_id

    async def pending(self, now: datetime, limit: int = 50) -> list[Notification]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM alert_notifications"
            " WHERE status = ANY($1)"
            " AND (next_try_at IS NULL OR next_try_at <= $2)"
            " ORDER BY id LIMIT $3",
            [QUEUED, HELD, FAILED],
            now,
            limit,
        )
        return [self._to_notification(row) for row in rows]

    async def for_alert(self, alert_id: int, statuses: list[str]) -> list[Notification]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM alert_notifications"
            " WHERE alert_id = $1 AND status = ANY($2) ORDER BY id",
            alert_id,
            statuses,
        )
        return [self._to_notification(row) for row in rows]

    async def last_sent(self, alert_id: int) -> datetime | None:
        value = await self._pool.fetchval(
            "SELECT max(updated_at) FROM alert_notifications WHERE alert_id = $1 AND status = $2",
            alert_id,
            SENT,
        )
        return value

    async def sent_between(self, start: datetime, end: datetime) -> int:
        """How many notifications actually landed in a window — the daily
        cap reads this, never a guess from the queue."""
        value = await self._pool.fetchval(
            "SELECT count(*) FROM alert_notifications"
            " WHERE status = $1 AND updated_at >= $2 AND updated_at < $3",
            SENT,
            start,
            end,
        )
        return int(value or 0)

    async def mark_sent(self, notification_ids: list[int]) -> None:
        if not notification_ids:
            return
        await self._pool.execute(
            "UPDATE alert_notifications SET status = $1, updated_at = now() WHERE id = ANY($2)",
            SENT,
            notification_ids,
        )

    async def mark_held(self, notification_ids: list[int], until: datetime) -> None:
        if not notification_ids:
            return
        await self._pool.execute(
            "UPDATE alert_notifications SET status = $1, next_try_at = $2,"
            " updated_at = now() WHERE id = ANY($3)",
            HELD,
            until,
            notification_ids,
        )

    async def mark_suppressed(self, notification_ids: list[int], reason: str) -> None:
        if not notification_ids:
            return
        await self._pool.execute(
            "UPDATE alert_notifications SET status = $1, reason = $2,"
            " updated_at = now() WHERE id = ANY($3)",
            SUPPRESSED,
            reason,
            notification_ids,
        )

    async def mark_failed(self, notification_ids: list[int], *, next_try_at: datetime) -> None:
        if not notification_ids:
            return
        await self._pool.execute(
            "UPDATE alert_notifications SET status = $1, attempts = attempts + 1,"
            " next_try_at = $2, updated_at = now() WHERE id = ANY($3)",
            FAILED,
            next_try_at,
            notification_ids,
        )

    async def recent(self, limit: int = 30) -> list[Notification]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM alert_notifications ORDER BY id DESC LIMIT $1", limit
        )
        return [self._to_notification(row) for row in rows]

    def _to_notification(self, row: asyncpg.Record) -> Notification:
        notification_id = int(row["id"])
        event = AlertEvent(alert_id=int(row["alert_id"]), event_id=0, source="", kind="")
        try:
            event = AlertEvent.from_payload(
                self._cipher.decrypt_json(row["event_enc"], aad=_aad("event_enc", notification_id))
            )
        except Exception:
            log.warning(
                "notification %s snapshot undecryptable — treated as empty", notification_id
            )
        return Notification(
            id=notification_id,
            alert_id=int(row["alert_id"]),
            event_id=int(row["event_id"] or 0),
            status=row["status"],
            style=row["style"],
            urgent=int(row["urgent"] or 0),
            attempts=int(row["attempts"] or 0),
            next_try_at=row["next_try_at"],
            event=event,
            created_at=row["created_at"],
        )
