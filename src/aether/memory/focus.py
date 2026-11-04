"""The focus object: what the last turn was about.

One row per chat, keyed by a blind token over the chat reference. The state
is sealed — it names the user's people, their last offer and their last
attachment — and it exists so a later turn can resolve "it", "that", "send
then" and "the assignment" without a search.

This layer only persists and maintains the object. The deterministic
reference resolution that reads it belongs to the query router; this module
is the storage and the bounded shape, not the resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .crypto import Cipher

log = logging.getLogger("aether.memory.focus")

# The object is a working set, not a history: the last handful of people the
# turn named, the one previous offer, the one open task, the one attachment.
MAX_RECENT_ENTITIES = 8


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# `LEAVE` is "this turn said nothing about the field" — distinct from an
# explicit None, which clears it (the open task was closed).
LEAVE = object()


@dataclass
class FocusState:
    """The sealed contents of one focus row."""

    recent_entities: list[int] = field(default_factory=list)
    last_offer: dict[str, Any] | None = None
    open_task: dict[str, Any] | None = None
    last_attachment: dict[str, Any] | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_payload(self) -> dict[str, Any]:
        return {
            "recent_entities": [int(e) for e in self.recent_entities[:MAX_RECENT_ENTITIES]],
            "last_offer": self.last_offer,
            "open_task": self.open_task,
            "last_attachment": self.last_attachment,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, data: Any) -> FocusState:
        """Whatever shape an older writer left, coerced — a malformed field
        degrades to empty rather than taking a turn down."""
        if not isinstance(data, dict):
            return cls()
        entities: list[int] = []
        for item in data.get("recent_entities") or []:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value not in entities:
                entities.append(value)
        return cls(
            recent_entities=entities[:MAX_RECENT_ENTITIES],
            last_offer=_dict_or_none(data.get("last_offer")),
            open_task=_dict_or_none(data.get("open_task")),
            last_attachment=_dict_or_none(data.get("last_attachment")),
            updated_at=_optional_time(data.get("updated_at")) or datetime.now(UTC),
        )


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


class Focus:
    """Database-backed store for one chat's focus object."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    def _token(self, chat_ref: str) -> bytes:
        return self._cipher.blind_token("focus", chat_ref or "")

    def _aad(self, token: bytes) -> str:
        return f"focus:state_enc:{token.hex()}"

    async def get(self, chat_ref: str) -> FocusState | None:
        """The current state, or None when there is none (or it cannot be
        read — a lost object costs a reference, never the turn)."""
        token = self._token(chat_ref)
        row = await self._pool.fetchrow(
            "SELECT state_enc FROM focus WHERE chat_ref_token = $1", token
        )
        if row is None:
            return None
        try:
            return FocusState.from_payload(
                self._cipher.decrypt_json(row["state_enc"], aad=self._aad(token))
            )
        except Exception:
            log.warning("focus state undecryptable — treated as empty")
            return None

    async def save(self, chat_ref: str, state: FocusState) -> None:
        token = self._token(chat_ref)
        state.updated_at = datetime.now(UTC)
        await self._pool.execute(
            "INSERT INTO focus (chat_ref_token, state_enc, key_version, updated_at)"
            " VALUES ($1, $2, $3, now())"
            " ON CONFLICT (chat_ref_token) DO UPDATE SET"
            " state_enc = EXCLUDED.state_enc, key_version = EXCLUDED.key_version,"
            " updated_at = now()",
            token,
            self._cipher.encrypt_json(state.to_payload(), aad=self._aad(token)),
            self._cipher.key_id,
        )

    async def record_turn(
        self,
        chat_ref: str,
        *,
        entities: list[int] | None = None,
        offer: dict[str, Any] | None = None,
        open_task: dict[str, Any] | None | object = LEAVE,
        attachment: dict[str, Any] | None = None,
    ) -> FocusState:
        """Fold one finished turn into the object.

        The people the turn named are merged to the front (newest first,
        capped). The offer and the attachment replace their previous value
        only when this turn produced one, so a turn that said nothing about
        the attachment does not erase it. `open_task` is `LEAVE` when the
        turn did not resolve it, and an explicit value (including None, when
        nothing is open) otherwise.
        """
        state = await self.get(chat_ref) or FocusState()
        if entities:
            merged: list[int] = []
            for entity_id in list(entities) + list(state.recent_entities):
                try:
                    value = int(entity_id)
                except (TypeError, ValueError):
                    continue
                if value not in merged:
                    merged.append(value)
            state.recent_entities = merged[:MAX_RECENT_ENTITIES]
        if offer is not None:
            state.last_offer = offer
        if attachment is not None:
            state.last_attachment = attachment
        if open_task is not LEAVE:
            state.open_task = open_task  # type: ignore[assignment]
        await self.save(chat_ref, state)
        return state
