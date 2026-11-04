"""Delivery records: one row per outbound send attempt (plan §8, C5).

The old fanout logged a failed surface and carried on, and the transcript
recorded a success either way — so a reply that never reached the user was
rendered as if it had. This store makes every attempt durable: queued before
the send, then sent (with the platform's message id) or failed (with the
error sealed, never logged in the clear).

Only the status vocabulary is plaintext. The body, the message id and the
error are sealed, so the record of a delivery cannot leak the words it
carried.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from .crypto import Cipher

log = logging.getLogger("aether.memory.deliveries")

# status vocabulary (plaintext)
QUEUED = "queued"
SENT = "sent"
FAILED = "failed"
ABANDONED = "abandoned"

_COLUMNS = "id, surface_token, status, body_enc, message_id_enc, error_enc, created_at"


@dataclass
class Delivery:
    id: int
    surface: str
    status: str
    body: str
    message_id: str
    error: str
    created_at: datetime


def _aad(column: str, delivery_id: int) -> str:
    return f"deliveries:{column}:{delivery_id}"


class Deliveries:
    """The sealed outbound-send log."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog | None = None) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def record_queued(self, surface: str, text: str) -> Delivery | None:
        """Open one attempt. An empty body is refused, never stored — the
        same rule the fanout enforces before it calls this (plan §8)."""
        body = (text or "").strip()
        if not body:
            return None
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO deliveries (surface_token, status, body_enc, key_version)"
                " VALUES ($1, $2, $3, $4) RETURNING id, created_at",
                self._cipher.blind_token("delivery", surface or ""),
                QUEUED,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._cipher.key_id,
            )
            delivery_id = row["id"]
            await conn.execute(
                "UPDATE deliveries SET body_enc = $1 WHERE id = $2",
                self._cipher.encrypt_json(
                    {"text": body, "surface": surface},
                    aad=_aad("body_enc", delivery_id),
                ),
                delivery_id,
            )
        return Delivery(
            id=delivery_id,
            surface=surface,
            status=QUEUED,
            body=body,
            message_id="",
            error="",
            created_at=row["created_at"],
        )

    async def mark_sent(self, delivery_id: int, message_id: str = "") -> None:
        await self._mark(delivery_id, SENT, message_id=message_id)

    async def mark_failed(self, delivery_id: int, error: str) -> None:
        await self._mark(delivery_id, FAILED, error=error)

    async def mark_abandoned(self, delivery_id: int, error: str = "") -> None:
        """A failure that has already been retried once: keep the record,
        stop re-delivering it on every later turn."""
        await self._mark(delivery_id, ABANDONED, error=error)

    async def _mark(
        self, delivery_id: int, status: str, *, message_id: str = "", error: str = ""
    ) -> None:
        if status not in (QUEUED, SENT, FAILED, ABANDONED):
            raise ValueError(f"unknown delivery status: {status!r}")
        assignments = ["status = $1"]
        values: list[Any] = [status]
        if message_id:
            values.append(
                self._cipher.encrypt_text(message_id, aad=_aad("message_id_enc", delivery_id))
            )
            assignments.append(f"message_id_enc = ${len(values)}")
        if error:
            # an executor error can quote the content it was carrying; only
            # the hash reaches the log, and the full text only the cipher
            values.append(self._cipher.encrypt_text(error, aad=_aad("error_enc", delivery_id)))
            assignments.append(f"error_enc = ${len(values)}")
            log.info(
                "delivery %d %s (error digest %s)",
                delivery_id,
                status,
                hashlib.sha256(error.encode("utf-8")).hexdigest()[:12],
            )
        values.append(delivery_id)
        await self._pool.execute(
            f"UPDATE deliveries SET {', '.join(assignments)} WHERE id = ${len(values)}",
            *values,
        )

    async def get(self, delivery_id: int) -> Delivery | None:
        row = await self._pool.fetchrow(
            f"SELECT {_COLUMNS} FROM deliveries WHERE id = $1", delivery_id
        )
        return self._to_delivery(row) if row else None

    async def undelivered(self, limit: int = 10) -> list[Delivery]:
        """Attempts that never landed — failed first (they need the retry),
        then still-queued rows from a send that died mid-flight."""
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM deliveries"
            " WHERE status IN ($1, $2)"
            " ORDER BY CASE status WHEN $1 THEN 0 ELSE 1 END, id DESC LIMIT $3",
            FAILED,
            QUEUED,
            limit,
        )
        return [self._to_delivery(row) for row in rows]

    async def recent(self, limit: int = 20) -> list[Delivery]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM deliveries ORDER BY id DESC LIMIT $1", limit
        )
        return [self._to_delivery(row) for row in rows]

    def _to_delivery(self, row: asyncpg.Record) -> Delivery:
        body_data = _open_json(self._cipher, row.get("body_enc"), _aad("body_enc", row["id"]))
        body = str(body_data.get("text") or "")
        surface = str(body_data.get("surface") or "")
        message_id = _open(
            self._cipher, row.get("message_id_enc"), _aad("message_id_enc", row["id"])
        )
        error = _open(self._cipher, row.get("error_enc"), _aad("error_enc", row["id"]))
        return Delivery(
            id=row["id"],
            surface=surface,
            status=row["status"],
            body=body,
            message_id=message_id,
            error=error,
            created_at=row["created_at"],
        )


def _open(cipher: Cipher, blob: Any, aad: str) -> str:
    if not blob:
        return ""
    try:
        return cipher.decrypt_text(blob, aad=aad)
    except Exception:
        log.warning("delivery field undecryptable — treated as empty")
        return ""


def _open_json(cipher: Cipher, blob: Any, aad: str) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = cipher.decrypt_json(blob, aad=aad)
    except Exception:
        log.warning("delivery body undecryptable — treated as empty")
        return {}
    return data if isinstance(data, dict) else {}
