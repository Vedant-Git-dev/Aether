from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..vault.scope import KeyScope
from .rows import CipherRows, Rows, ScopedRows
from .storage import SealedStore, delete_held_notes, held_note_rows

log = logging.getLogger("aether.memory.held_notes")


@dataclass(frozen=True)
class HeldNote:
    id: int
    text: str


class HeldNotes(Protocol):
    async def hold(self, text: str) -> None: ...
    async def pending(self, limit: int = 100) -> list[HeldNote]: ...
    async def release(self, note_ids: list[int]) -> None: ...


class MemoryHeldNotes:
    def __init__(self) -> None:
        self._rows: dict[int, str] = {}
        self._next_id = 1

    async def hold(self, text: str) -> None:
        self._rows[self._next_id] = text
        self._next_id += 1

    async def pending(self, limit: int = 100) -> list[HeldNote]:
        return [HeldNote(id=key, text=text) for key, text in sorted(self._rows.items())][:limit]

    async def release(self, note_ids: list[int]) -> None:
        for note_id in note_ids:
            self._rows.pop(note_id, None)


class DbHeldNotes:
    def __init__(self, pool: Any, cipher: Any = None, *, rows: Rows | None = None) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("DbHeldNotes needs exactly one of cipher or rows")
        self._pool = pool
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._sealed = SealedStore(pool, rows=self._rows)

    @classmethod
    def scoped(cls, pool: Any, scope: KeyScope, *, ver: int = 1) -> DbHeldNotes:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, rows=ScopedRows(scope, cat="memory", ver=ver))

    async def hold(self, text: str) -> None:
        self._sealed.preflight()
        await self._sealed.insert("held_notes", sealed={"body_enc": text})

    async def pending(self, limit: int = 100) -> list[HeldNote]:
        self._sealed.preflight()
        rows = await held_note_rows(self._pool, limit)
        return [HeldNote(id=int(row["id"]), text=self.open_body(row)) for row in rows]

    async def release(self, note_ids: list[int]) -> None:
        self._sealed.preflight()
        if note_ids:
            await delete_held_notes(self._pool, list(note_ids))

    def open_body(self, row: Any) -> str:
        try:
            body = self._sealed.unseal(
                row["body_enc"], table="held_notes", column="body_enc", row_id=row["id"]
            )
        except Exception:
            if self._scoped:
                raise
            log.warning("held note %s undecryptable, released without sending", row["id"])
            return ""
        return body if isinstance(body, str) else ""
