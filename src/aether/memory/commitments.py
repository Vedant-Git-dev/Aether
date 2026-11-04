"""Commitments: what the user owes, and what they are owed.

Plan §3 and §4.3. A commitment is extracted at ingest — greeted by the
classifier alongside urgency and deadlines — and stored here so "what did
I promise X" and "what's due" are lookups rather than searches.

The row's vocabulary (`kind`, `status`, the counterparty and the source
event) stays plaintext because it is a status class, not content. What the
commitment actually *is* — its text, and the time it is due — is sealed,
which is why the open set is read once and ordered in memory: Postgres
cannot sort by a due time it is not allowed to read.

The store mirrors `Scheduler`'s insert-then-seal shape so the cipher's AAD
can bind to the row id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from .crypto import Cipher

log = logging.getLogger("aether.memory.commitments")

# status vocabulary (plaintext)
OPEN = "open"
DONE = "done"
CANCELLED = "cancelled"

# kind vocabulary (plaintext) — the four shapes plan §3 names
KINDS = ("task", "request", "promise", "awaiting_reply")


@dataclass
class Commitment:
    id: int
    kind: str
    status: str
    text: str
    due_at: datetime | None
    counterparty_identity_id: int | None
    source_event_id: int | None
    created_at: datetime


def _aad(commitment_id: int) -> str:
    return f"commitments:body_enc:{commitment_id}"


class Commitments:
    """Database-backed store for the commitments extracted from events."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def add(
        self,
        *,
        kind: str,
        text: str,
        due_at: datetime | None = None,
        due_phrase: str = "",
        counterparty_identity_id: int | None = None,
        source_event_id: int | None = None,
        actor: str = "system",
    ) -> Commitment | None:
        """One commitment, sealed. An empty text is a bug upstream, not a
        row: it is refused rather than stored as a blank promise."""
        body = (text or "").strip()
        if not body:
            return None
        kind = kind if kind in KINDS else "task"
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO commitments (kind, status, body_enc, key_version,"
                " counterparty_identity_id, source_event_id)"
                " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at",
                kind,
                OPEN,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._cipher.key_id,
                counterparty_identity_id,
                source_event_id,
            )
            commitment_id = row["id"]
            await conn.execute(
                "UPDATE commitments SET body_enc = $1 WHERE id = $2",
                self._cipher.encrypt_json(
                    {
                        "text": body[:400],
                        "due_at": due_at.astimezone(UTC).isoformat() if due_at else None,
                        "due_phrase": due_phrase,
                    },
                    aad=_aad(commitment_id),
                ),
                commitment_id,
            )
        await self._audit.append(
            actor=actor,
            tool_name="commitment",
            decision="info",
            params={"kind": kind, "counterparty_identity_id": counterparty_identity_id},
            outcome="commitment recorded",
        )
        return Commitment(
            id=commitment_id,
            kind=kind,
            status=OPEN,
            text=body,
            due_at=due_at,
            counterparty_identity_id=counterparty_identity_id,
            source_event_id=source_event_id,
            created_at=row["created_at"],
        )

    async def add_many(
        self,
        items: list[dict[str, Any]],
        *,
        source_event_id: int | None = None,
        counterparty_identity_id: int | None = None,
        actor: str = "system",
    ) -> list[Commitment]:
        """Every commitment one event yielded. One bad item never costs the
        others — a failure is logged and skipped."""
        out: list[Commitment] = []
        for item in items:
            try:
                commitment = await self.add(
                    kind=str(item.get("kind") or "task"),
                    text=str(item.get("text") or ""),
                    due_at=item.get("due_at"),
                    due_phrase=str(item.get("due_phrase") or ""),
                    counterparty_identity_id=counterparty_identity_id,
                    source_event_id=source_event_id,
                    actor=actor,
                )
            except Exception:
                log.warning("could not store a commitment from event %s", source_event_id)
                continue
            if commitment is not None:
                out.append(commitment)
        return out

    async def get(self, commitment_id: int) -> Commitment | None:
        row = await self._pool.fetchrow(
            "SELECT id, kind, status, body_enc, counterparty_identity_id,"
            " source_event_id, created_at FROM commitments WHERE id = $1",
            commitment_id,
        )
        return self._to_commitment(row) if row else None

    async def list_open(self, limit: int = 200) -> list[Commitment]:
        """The open set, soonest due first.

        Ordered here rather than in SQL because the due time is sealed —
        and undated commitments sort last, since "no deadline" is not
        "due now".
        """
        rows = await self._pool.fetch(
            "SELECT id, kind, status, body_enc, counterparty_identity_id,"
            " source_event_id, created_at FROM commitments"
            " WHERE status = $1 ORDER BY id DESC LIMIT $2",
            OPEN,
            limit,
        )
        found = [self._to_commitment(row) for row in rows]
        found.sort(key=lambda c: (c.due_at is None, c.due_at or c.created_at))
        return found

    async def for_identity(self, identity_id: int, limit: int = 50) -> list[Commitment]:
        rows = await self._pool.fetch(
            "SELECT id, kind, status, body_enc, counterparty_identity_id,"
            " source_event_id, created_at FROM commitments"
            " WHERE counterparty_identity_id = $1 ORDER BY id DESC LIMIT $2",
            identity_id,
            limit,
        )
        found = [self._to_commitment(row) for row in rows]
        found.sort(key=lambda c: (c.due_at is None, c.due_at or c.created_at))
        return found

    async def resolve(self, commitment_id: int, status: str = DONE) -> bool:
        """Close one out. Status only — the body is never rewritten, so
        what was promised survives being done."""
        if status not in (DONE, CANCELLED, OPEN):
            raise ValueError(f"unknown commitment status: {status!r}")
        result = await self._pool.execute(
            "UPDATE commitments SET status = $1 WHERE id = $2 AND status <> $1",
            status,
            commitment_id,
        )
        return result.endswith(" 1")

    def _to_commitment(self, row: asyncpg.Record) -> Commitment:
        body: dict[str, Any] = {}
        try:
            body = self._cipher.decrypt_json(row["body_enc"], aad=_aad(row["id"]))
        except Exception:
            log.warning("commitment %s body undecryptable — treated as empty", row["id"])
        return Commitment(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            text=str(body.get("text", "")),
            due_at=_optional_time(body.get("due_at")),
            counterparty_identity_id=row["counterparty_identity_id"],
            source_event_id=row["source_event_id"],
            created_at=row["created_at"],
        )


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
