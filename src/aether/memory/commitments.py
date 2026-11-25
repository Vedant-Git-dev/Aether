"""Commitments: what the user owes, and what they are owed.

A commitment is extracted at ingest — greeted by the
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
from ..vault.scope import KeyScope
from .crypto import Cipher
from .rows import CipherRows, Rows, ScopedRows

log = logging.getLogger("aether.memory.commitments")

# status vocabulary (plaintext)
OPEN = "open"
DONE = "done"
CANCELLED = "cancelled"

# kind vocabulary (plaintext) — the four commitment shapes
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
    urgency: int = 0
    # the artifacts the commitment is tied to — the thread it lives in, the
    # document it names, the stored file it points at — so "the assignment"
    # can resolve to one concrete thing instead of a search
    thread_id: str = ""
    document: str = ""
    media_id: int | None = None


def _aad(commitment_id: int) -> str:
    return f"commitments:body_enc:{commitment_id}"


class Commitments:
    """Database-backed store for the commitments extracted from events."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher | None,
        audit: AuditLog,
        *,
        rows: Rows | None = None,
    ) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("Commitments needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool
        self._audit = audit

    @classmethod
    def scoped(
        cls,
        pool: asyncpg.Pool,
        scope: KeyScope,
        audit: AuditLog,
        *,
        ver: int = 1,
    ) -> Commitments:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, None, audit, rows=ScopedRows(scope, cat="memory", ver=ver))

    async def add(
        self,
        *,
        kind: str,
        text: str,
        due_at: datetime | None = None,
        due_phrase: str = "",
        counterparty_identity_id: int | None = None,
        source_event_id: int | None = None,
        urgency: int = 0,
        thread_id: str = "",
        document: str = "",
        media_id: int | None = None,
        actor: str = "system",
    ) -> Commitment | None:
        """One commitment, sealed. An empty text is a bug upstream, not a
        row: it is refused rather than stored as a blank promise."""
        self._rows.preflight()
        body = (text or "").strip()
        if not body:
            return None
        kind = kind if kind in KINDS else "task"
        urgency = max(0, min(3, int(urgency or 0)))
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO commitments (kind, status, body_enc, key_version,"
                " counterparty_identity_id, source_event_id)"
                " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at",
                kind,
                OPEN,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._rows.key_version,
                counterparty_identity_id,
                source_event_id,
            )
            commitment_id = row["id"]
            await conn.execute(
                "UPDATE commitments SET body_enc = $1 WHERE id = $2",
                self._rows.seal(
                    {
                        "text": body[:400],
                        "due_at": due_at.astimezone(UTC).isoformat() if due_at else None,
                        "due_phrase": due_phrase,
                        "urgency": urgency,
                        # links through entity (counterparty), thread and
                        # document name; every value is sealed here
                        "thread_id": str(thread_id or "")[:200],
                        "document": str(document or "")[:300],
                        "media_id": int(media_id) if media_id else None,
                    },
                    table="commitments",
                    column="body_enc",
                    row_id=commitment_id,
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
            urgency=urgency,
            thread_id=str(thread_id or ""),
            document=str(document or ""),
            media_id=int(media_id) if media_id else None,
        )

    async def add_many(
        self,
        items: list[dict[str, Any]],
        *,
        source_event_id: int | None = None,
        counterparty_identity_id: int | None = None,
        urgency: int = 0,
        thread_id: str = "",
        document: str = "",
        media_id: int | None = None,
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
                    urgency=int(item.get("urgency") or urgency),
                    thread_id=thread_id,
                    document=document,
                    media_id=media_id,
                    actor=actor,
                )
            except Exception:
                log.warning("could not store a commitment from event %s", source_event_id)
                continue
            if commitment is not None:
                out.append(commitment)
        return out

    async def get(self, commitment_id: int) -> Commitment | None:
        self._rows.preflight()
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
        self._rows.preflight()
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
        self._rows.preflight()
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
        self._rows.preflight()
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
            body = self._rows.open(
                row["body_enc"],
                table="commitments",
                column="body_enc",
                row_id=row["id"],
                aad=_aad(row["id"]),
            )
        except Exception:
            if self._scoped:
                raise
            log.warning("commitment %s body undecryptable — treated as empty", row["id"])
        try:
            urgency = max(0, min(3, int(body.get("urgency") or 0)))
        except (TypeError, ValueError):
            urgency = 0
        return Commitment(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            text=str(body.get("text", "")),
            due_at=_optional_time(body.get("due_at")),
            counterparty_identity_id=row["counterparty_identity_id"],
            source_event_id=row["source_event_id"],
            created_at=row["created_at"],
            urgency=urgency,
            thread_id=str(body.get("thread_id") or ""),
            document=str(body.get("document") or ""),
            media_id=_optional_int(body.get("media_id")),
        )


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
