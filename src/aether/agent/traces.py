"""Decision traces — the encrypted "why" behind every action.

The audit log is the *what*: append-only, hash-chained, parameters kept
as digests so the chain proves what was decided without storing what was
in the call. A trace is the *why*: what triggered the act, what the model
saw and said, how the authorization gate ruled each proposed call, and
what came back. Together they answer "why did you do that?" with a record
instead of a reconstruction.

One row per act, four kinds:
- turn      — one LLM turn: the trigger (messages, observations), every
              gated call with its ruling, the model's own words between
              calls, and its reply
- alert     — one alert fire: the stored condition, the matching event,
              the call it ran (the legacy kind `routine` still reads)
- scheduled — one scheduled action firing: the label, the call, the result
- carry_out — an approved call running: the approval, the result

Traces are encrypted at rest like all human-readable content, and they
are bookkeeping, not decisions: writing one must never break the act it
records, so the loop wraps every trace write in its own try/except.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from ..memory.crypto import Cipher
from ..memory.rows import CipherRows, Rows, ScopedRows
from ..vault.scope import KeyScope

log = logging.getLogger("aether.agent.traces")

# what a trace row can be
TURN = "turn"
ALERT = "alert"
ROUTINE = "routine"  # legacy rows only; new fires use ALERT
SCHEDULED = "scheduled"
CARRY_OUT = "carry_out"


def _aad(trace_id: int) -> str:
    return f"decision_traces:trace_enc:{trace_id}"


@dataclass
class Trace:
    id: int
    kind: str
    label: str
    payload: dict[str, Any]  # decrypted
    created_at: datetime


class Traces:
    """Database-backed store for decision traces."""

    def __init__(
        self, pool: asyncpg.Pool, cipher: Cipher | None, *, rows: Rows | None = None
    ) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("Traces needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool
        # a row written under an older content key is unreadable forever;
        # warning on every panel read would flood the log, so each bad id
        # gets one line per process
        self._warned: set[int] = set()

    @classmethod
    def scoped(cls, pool: asyncpg.Pool, scope: KeyScope, *, ver: int = 1) -> Traces:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, None, rows=ScopedRows(scope, cat="operations", ver=ver))

    async def create(self, *, kind: str, label: str = "", payload: dict[str, Any]) -> Trace:
        """Persist one trace — the same two-step encrypt pattern as every
        store: insert a placeholder, then write ciphertext bound to the
        row id via AAD, in the same transaction."""
        self._rows.preflight()
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO decision_traces (kind, trace_enc, key_version)"
                " VALUES ($1, $2, $3) RETURNING id, created_at",
                kind,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._rows.key_version,
            )
            trace_id = row["id"]
            await conn.execute(
                "UPDATE decision_traces SET trace_enc = $1, label_enc = $2 WHERE id = $3",
                self._rows.seal(
                    payload,
                    table="decision_traces",
                    column="trace_enc",
                    row_id=trace_id,
                    aad=_aad(trace_id),
                ),
                self._rows.seal_bytes(
                    label.encode("utf-8"),
                    table="decision_traces",
                    column="label_enc",
                    row_id=trace_id,
                    aad=f"decision_traces:label_enc:{trace_id}",
                ),
                trace_id,
            )
        return Trace(
            id=trace_id,
            kind=kind,
            label=label,
            payload=dict(payload),
            created_at=row["created_at"],
        )

    async def get(self, trace_id: int) -> Trace | None:
        self._rows.preflight()
        row = await self._pool.fetchrow(
            "SELECT id, kind, label_enc, trace_enc, created_at FROM decision_traces WHERE id = $1",
            trace_id,
        )
        return self._to_trace(row) if row else None

    async def recent(self, limit: int = 20) -> list[Trace]:
        """Newest first — the explain tool's and the web panel's view."""
        self._rows.preflight()
        rows = await self._pool.fetch(
            "SELECT id, kind, label_enc, trace_enc, created_at FROM decision_traces"
            " ORDER BY id DESC LIMIT $1",
            limit,
        )
        return [self._to_trace(r) for r in rows]

    async def recent_summaries(self, limit: int = 30, offset: int = 0) -> list[Trace]:
        """Newest first, header only — the panel list never needs the payload.

        Decrypting every trace body to render a one-line label was most of
        the trace list's cost. The detail route still reads the full row.
        """
        self._rows.preflight()
        rows = await self._pool.fetch(
            "SELECT id, kind, label_enc, created_at FROM decision_traces"
            " ORDER BY id DESC LIMIT $1 OFFSET $2",
            limit,
            max(0, offset),
        )
        return [
            Trace(
                id=row["id"],
                kind=row["kind"],
                label=self._label(row),
                payload={},
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _label(self, row: asyncpg.Record) -> str:
        blob = row.get("label_enc")
        if blob:
            try:
                return self._rows.open_bytes(
                    blob,
                    table="decision_traces",
                    column="label_enc",
                    row_id=row["id"],
                    aad=f"decision_traces:label_enc:{row['id']}",
                ).decode("utf-8")
            except Exception:
                if self._scoped:
                    raise
                if row["id"] not in self._warned:
                    self._warned.add(row["id"])
                    log.warning("trace %s label undecryptable — treated as empty", row["id"])
        return ""

    def _to_trace(self, row: asyncpg.Record) -> Trace:
        trace_id = row["id"]
        try:
            payload = self._rows.open(
                row["trace_enc"],
                table="decision_traces",
                column="trace_enc",
                row_id=trace_id,
                aad=_aad(trace_id),
            )
        except Exception:
            if self._scoped:
                raise
            if trace_id not in self._warned:
                self._warned.add(trace_id)
                log.warning("trace %s undecryptable — treated as empty", trace_id)
            payload = {}
        return Trace(
            id=trace_id,
            kind=row["kind"],
            label=self._label(row),
            payload=payload,
            created_at=row["created_at"],
        )
