"""The inbound ledger: one terminal outcome per message a person sent.

An inbound message is ``received`` when it arrives, then becomes exactly
one of ``answered`` (the outbound message id recorded), ``failed`` (the
reason sealed) or ``expired`` (too old to answer unprompted). "Answered"
always means a recorded terminal outcome — never "the reply text was
non-empty". A failed or empty reply makes the inbound failed; it does not
leave it received.

The ledger is what proactive and unprompted turns read to decide whether
anything is still owed a reply — never the transcript, which cannot tell a
delivered answer from one that never went out.

Only keyed blind tokens and the fixed status vocabulary sit in plaintext;
the failure reason and the outbound id are sealed. An in-memory
implementation backs tests and single-process runs; the pool-backed one is
wired at boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger("aether.memory.inbound")

RECEIVED = "received"
ANSWERED = "answered"
FAILED = "failed"
EXPIRED = "expired"

_TERMINAL = frozenset({ANSWERED, FAILED, EXPIRED})
_STATUSES = frozenset({RECEIVED, *_TERMINAL})


@dataclass
class Inbound:
    id: int
    surface: str
    chat_ref: str
    message_id: str
    status: str
    reason: str
    outcome: str
    checked: bool
    created_at: datetime
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL


def inbound_key(surface: str, message_id: str, fallback: str) -> str:
    """The idempotency key for one inbound: its platform message id when
    there is one, else the caller's fallback (so an update re-delivered with
    the same message id is one row, and one turn)."""
    clean = (message_id or "").strip()
    return f"{surface}:{clean}" if clean else fallback


class InboundStates(Protocol):
    async def record(
        self, surface: str, chat_ref: str, message_id: str = "", *, key: str = ""
    ) -> Inbound | None: ...
    async def mark_answered(self, inbound_id: int, outcome: str = "") -> None: ...
    async def mark_failed(self, inbound_id: int, reason: str = "") -> None: ...
    async def mark_expired(self, inbound_id: int) -> None: ...
    async def mark_checked(self, inbound_id: int) -> None: ...
    async def get(self, inbound_id: int) -> Inbound | None: ...
    async def pending(self, limit: int = 20) -> list[Inbound]: ...
    async def stale_unchecked(self, cutoff: datetime, limit: int = 20) -> list[Inbound]: ...
    async def recent(self, limit: int = 20) -> list[Inbound]: ...


class MemoryInboundStates:
    """The ledger in process memory — the shape tests drive, and the
    fallback when no database store is wired. Behaviour matches the
    pool-backed one exactly; only durability differs."""

    def __init__(self) -> None:
        self._rows: dict[int, Inbound] = {}
        self._by_key: dict[str, int] = {}
        self._next_id = 1

    async def record(
        self, surface: str, chat_ref: str, message_id: str = "", *, key: str = ""
    ) -> Inbound | None:
        token = key or inbound_key(surface, message_id, f"{surface}:{self._next_id}")
        existing = self._by_key.get(token)
        if existing is not None:
            return None  # a duplicate of a message already recorded
        now = datetime.now(UTC)
        row = Inbound(
            id=self._next_id,
            surface=surface,
            chat_ref=chat_ref,
            message_id=message_id,
            status=RECEIVED,
            reason="",
            outcome="",
            checked=False,
            created_at=now,
            updated_at=now,
        )
        self._rows[row.id] = row
        self._by_key[token] = row.id
        self._next_id += 1
        return row

    async def _set(
        self, inbound_id: int, status: str, *, reason: str = "", outcome: str = ""
    ) -> None:
        row = self._rows.get(inbound_id)
        if row is None or row.terminal:
            return
        row.status = status
        if reason:
            row.reason = reason
        if outcome:
            row.outcome = outcome
        row.updated_at = datetime.now(UTC)

    async def mark_answered(self, inbound_id: int, outcome: str = "") -> None:
        await self._set(inbound_id, ANSWERED, outcome=outcome)

    async def mark_failed(self, inbound_id: int, reason: str = "") -> None:
        await self._set(inbound_id, FAILED, reason=reason)

    async def mark_expired(self, inbound_id: int) -> None:
        await self._set(inbound_id, EXPIRED)

    async def mark_checked(self, inbound_id: int) -> None:
        row = self._rows.get(inbound_id)
        if row is not None:
            row.checked = True
            row.updated_at = datetime.now(UTC)

    async def get(self, inbound_id: int) -> Inbound | None:
        return self._rows.get(inbound_id)

    async def pending(self, limit: int = 20) -> list[Inbound]:
        rows = [row for row in self._rows.values() if row.status == RECEIVED]
        rows.sort(key=lambda row: row.id)
        return rows[:limit]

    async def stale_unchecked(self, cutoff: datetime, limit: int = 20) -> list[Inbound]:
        rows = [
            row
            for row in self._rows.values()
            if row.status == RECEIVED and not row.checked and row.created_at <= cutoff
        ]
        rows.sort(key=lambda row: row.id)
        return rows[:limit]

    async def recent(self, limit: int = 20) -> list[Inbound]:
        rows = sorted(self._rows.values(), key=lambda row: row.id, reverse=True)
        return rows[:limit]


_COLUMNS = (
    "id, surface, chat_token, status, checked, reason_enc, outcome_enc, created_at, updated_at"
)


class DbInboundStates:
    """The durable sealed ledger. Every user-derived value is ciphertext
    under an AAD bound to the row id; only blind tokens and status words are
    plaintext."""

    def __init__(self, pool: Any, cipher: Any) -> None:
        self._pool = pool
        self._cipher = cipher

    def _token(self, purpose: str, value: str) -> bytes:
        return self._cipher.blind_token(f"inbound-{purpose}", value)

    async def record(
        self, surface: str, chat_ref: str, message_id: str = "", *, key: str = ""
    ) -> Inbound | None:
        token = key or inbound_key(
            surface, message_id, f"{surface}:{datetime.now(UTC).timestamp()}"
        )
        row = await self._pool.fetchrow(
            "INSERT INTO inbound_messages"
            " (surface, chat_token, message_token, key_token, status, key_version)"
            " VALUES ($1, $2, $3, $4, $5, $6)"
            " ON CONFLICT (key_token) DO NOTHING"
            " RETURNING id, created_at, updated_at",
            surface,
            self._token("chat", chat_ref or surface),
            self._token("message", message_id or ""),
            self._token("key", token),
            RECEIVED,
            self._cipher.key_id,
        )
        if row is None:
            return None
        return await self.get(int(row["id"]))

    async def _set(
        self, inbound_id: int, status: str, *, reason: str = "", outcome: str = ""
    ) -> None:
        if status not in _STATUSES:
            raise ValueError(f"unknown inbound status: {status!r}")
        await self._pool.execute(
            "UPDATE inbound_messages SET status = $1, updated_at = now()"
            " WHERE id = $2 AND status = $3",
            status,
            inbound_id,
            RECEIVED,
        )
        if reason:
            await self._pool.execute(
                "UPDATE inbound_messages SET reason_enc = $1 WHERE id = $2",
                self._cipher.encrypt_text(reason, aad=self._aad("reason_enc", inbound_id)),
                inbound_id,
            )
        if outcome:
            await self._pool.execute(
                "UPDATE inbound_messages SET outcome_enc = $1 WHERE id = $2",
                self._cipher.encrypt_text(outcome, aad=self._aad("outcome_enc", inbound_id)),
                inbound_id,
            )

    def _aad(self, column: str, inbound_id: int) -> str:
        return f"inbound_messages:{column}:{inbound_id}"

    async def mark_answered(self, inbound_id: int, outcome: str = "") -> None:
        await self._set(inbound_id, ANSWERED, outcome=outcome)

    async def mark_failed(self, inbound_id: int, reason: str = "") -> None:
        await self._set(inbound_id, FAILED, reason=reason)

    async def mark_expired(self, inbound_id: int) -> None:
        await self._set(inbound_id, EXPIRED)

    async def mark_checked(self, inbound_id: int) -> None:
        await self._pool.execute(
            "UPDATE inbound_messages SET checked = true, updated_at = now() WHERE id = $1",
            inbound_id,
        )

    async def get(self, inbound_id: int) -> Inbound | None:
        row = await self._pool.fetchrow(
            f"SELECT {_COLUMNS} FROM inbound_messages WHERE id = $1", inbound_id
        )
        return self._to_inbound(row) if row is not None else None

    async def pending(self, limit: int = 20) -> list[Inbound]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM inbound_messages WHERE status = $1 ORDER BY id ASC LIMIT $2",
            RECEIVED,
            limit,
        )
        return [self._to_inbound(row) for row in rows]

    async def stale_unchecked(self, cutoff: datetime, limit: int = 20) -> list[Inbound]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM inbound_messages"
            " WHERE status = $1 AND checked = false AND created_at <= $2"
            " ORDER BY id ASC LIMIT $3",
            RECEIVED,
            cutoff,
            limit,
        )
        return [self._to_inbound(row) for row in rows]

    async def recent(self, limit: int = 20) -> list[Inbound]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM inbound_messages ORDER BY id DESC LIMIT $1", limit
        )
        return [self._to_inbound(row) for row in rows]

    def _to_inbound(self, row: Any) -> Inbound:
        return Inbound(
            id=int(row["id"]),
            surface=str(row["surface"]),
            chat_ref="",
            message_id="",
            status=str(row["status"]),
            reason=self._open(row["reason_enc"], "reason_enc", int(row["id"])),
            outcome=self._open(row["outcome_enc"], "outcome_enc", int(row["id"])),
            checked=bool(row["checked"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _open(self, blob: Any, column: str, inbound_id: int) -> str:
        if not blob:
            return ""
        try:
            return self._cipher.decrypt_text(blob, aad=self._aad(column, inbound_id))
        except Exception:
            log.warning("inbound field undecryptable — treated as empty")
            return ""
