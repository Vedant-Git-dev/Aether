"""Cross-platform identity resolution and relationship notes.

The same person shows up as an email address, a Telegram handle, a Slack
id. Resolution: normalize the handle, try an exact match, then generate
fuzzy candidates (rapidfuzz over handles, display names, email locals,
phone digits), and let the LLM confirm; link at >= 0.8 confidence,
otherwise create a new identity. Links, creates, and merges are all
audited — resolution is never silent.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import asyncpg
from rapidfuzz import fuzz

from ..authz.audit import AuditLog
from ..llm.json_utils import extract_json
from ..llm.types import Message
from .crypto import Cipher

log = logging.getLogger("aether.memory.entities")

CONFIRM_THRESHOLD = 0.8
CANDIDATE_FLOOR = 75.0
CANDIDATE_POOL = 200
MAX_LLM_CANDIDATES = 3


# ---------------------------------------------------------------------------
# handle normalization (shared with the contact allowlist in events.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sender:
    platform: str
    handle: str


_PHONE_PLATFORMS = {"phone", "sms", "whatsapp", "call"}


def normalize_handle(platform: str, handle: str) -> str:
    """Canonical matching form.

    Emails and phone numbers are platform-agnostic (a mail rule matches a
    gmail sender); platform handles embed their platform, so a Telegram
    @vedant never collides with a Discord @vedant by itself.
    """
    platform = (platform or "").strip().lower()
    h = (handle or "").strip()
    if h.lower().startswith("mailto:"):
        h = h[7:]
    h = h.strip().lower()
    if not h:
        return ""
    digits = re.sub(r"\D", "", h)
    is_phone = platform in _PHONE_PLATFORMS or (
        h.startswith("+") and len(digits) >= 7
    )
    if is_phone:
        key = digits[-10:] if len(digits) >= 10 else digits
        return f"tel:{key}"
    if "@" in h and not h.startswith("@"):
        return f"email:{h}"
    return f"{platform}:{h.lstrip('@')}"


# ---------------------------------------------------------------------------
# data + judge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonRef:
    platform: str
    handle: str
    display_name: str = ""


@dataclass
class Entity:
    id: int
    display_name: str
    confidence: float
    handles: list[tuple[str, str]] = field(default_factory=list)  # (platform, raw_handle)


@dataclass
class EntityNote:
    id: int
    identity_id: int
    created_at: datetime
    payload: dict[str, Any]


class SamePersonJudge(Protocol):
    async def confirm(self, a: PersonRef, b: PersonRef) -> float:
        """Confidence 0-1 that two references are the same real person."""


class LLMSamePersonJudge:
    """Adapts any llm.Provider into an identity-confirmation judge."""

    PROMPT = (
        "You decide whether two contact references, possibly from different "
        "platforms, refer to the same real person. Consider names, email "
        "locals, phone digits, and platform handle conventions. Common "
        "first names with different handles are usually different people.\n"
        'Reply with strict JSON only: {"same_person": true|false, "confidence": <0-1>}'
    )

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def confirm(self, a: PersonRef, b: PersonRef) -> float:
        payload = {
            "reference_a": {"platform": a.platform, "handle": a.handle, "display_name": a.display_name},
            "reference_b": {"platform": b.platform, "handle": b.handle, "display_name": b.display_name},
        }
        turn = await self._provider.complete(
            self.PROMPT, [Message.user(json.dumps(payload))], tools=[]
        )
        data = extract_json(turn.text)
        confidence = float(data.get("confidence", 0.0))
        if not bool(data.get("same_person", False)):
            return 0.0
        return max(0.0, min(1.0, confidence))


# ---------------------------------------------------------------------------
# candidate generation (pure, unit-tested)
# ---------------------------------------------------------------------------


def candidate_score(new: PersonRef, existing: PersonRef) -> float:
    """Rough 0-100 similarity used only to pick candidates for LLM review —
    never to link on its own."""
    scores: list[float] = []
    if new.handle and existing.handle:
        scores.append(fuzz.partial_ratio(new.handle.lower(), existing.handle.lower()))
    if new.display_name and existing.display_name:
        scores.append(
            fuzz.token_set_ratio(new.display_name.lower(), existing.display_name.lower())
        )
    new_local, _, _ = new.handle.partition("@")
    old_local, _, _ = existing.handle.partition("@")
    if new_local and old_local and "@" in new.handle and "@" in existing.handle:
        scores.append(fuzz.ratio(new_local.lower(), old_local.lower()))
    new_digits = re.sub(r"\D", "", new.handle)
    old_digits = re.sub(r"\D", "", existing.handle)
    if len(new_digits) >= 7 and len(old_digits) >= 7:
        scores.append(
            100.0 if new_digits[-10:] == old_digits[-10:] else float(fuzz.ratio(new_digits, old_digits))
        )
    return max(scores) if scores else 0.0


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class Entities:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher,
        audit: AuditLog,
        judge: SamePersonJudge,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._judge = judge

    # -- reads ------------------------------------------------------------

    async def resolve_exact(self, platform: str, handle: str) -> Entity | None:
        normalized = normalize_handle(platform, handle)
        if not normalized:
            return None
        return await self._by_normalized(normalized)

    async def get(self, identity_id: int) -> Entity | None:
        row = await self._pool.fetchrow(
            "SELECT id, display_name, confidence FROM identities"
            " WHERE id = $1 AND merged_into_id IS NULL",
            identity_id,
        )
        if row is None:
            return None
        handles = await self._pool.fetch(
            "SELECT platform, raw_handle FROM identity_handles WHERE identity_id = $1",
            identity_id,
        )
        return Entity(
            id=row["id"],
            display_name=row["display_name"],
            confidence=row["confidence"],
            handles=[(h["platform"], h["raw_handle"]) for h in handles],
        )

    async def note(self, identity_id: int, payload: dict[str, Any]) -> None:
        """Append an encrypted relationship note for an identity."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                note_id = await conn.fetchval(
                    "INSERT INTO entity_notes (identity_id, payload_enc) VALUES ($1, $2) RETURNING id",
                    identity_id,
                    b"",  # placeholder until the id exists; replaced below, same transaction
                )
                blob = self._cipher.encrypt_json(
                    payload, aad=f"entity_notes:payload_enc:{note_id}"
                )
                await conn.execute(
                    "UPDATE entity_notes SET payload_enc = $1 WHERE id = $2",
                    blob,
                    note_id,
                )
        await self._audit.append(
            actor="agent",
            tool_name="note_entity",
            decision="info",
            params={"identity_id": identity_id, "note_kind": payload.get("kind", "")},
            outcome="entity note added",
        )

    async def notes_for(self, identity_id: int, limit: int = 10) -> list[EntityNote]:
        rows = await self._pool.fetch(
            "SELECT id, identity_id, created_at, payload_enc FROM entity_notes"
            " WHERE identity_id = $1 ORDER BY created_at DESC LIMIT $2",
            identity_id,
            limit,
        )
        notes: list[EntityNote] = []
        for row in rows:
            try:
                payload = self._cipher.decrypt_json(
                    row["payload_enc"], aad=f"entity_notes:payload_enc:{row['id']}"
                )
            except Exception:
                log.warning("note %s undecryptable — skipping", row["id"])
                continue
            notes.append(
                EntityNote(
                    id=row["id"],
                    identity_id=row["identity_id"],
                    created_at=row["created_at"],
                    payload=payload,
                )
            )
        return notes

    # -- resolution ---------------------------------------------------------

    async def resolve(
        self, platform: str, handle: str, display_name: str = ""
    ) -> Entity:
        """Link a reference to an identity — exact match, fuzzy candidates
        confirmed by the LLM, or a fresh identity."""
        normalized = normalize_handle(platform, handle)
        exact = await self._by_normalized(normalized)
        if exact is not None:
            return exact

        new = PersonRef(platform, handle, display_name)
        rows = await self._pool.fetch(
            "SELECT ih.platform, ih.raw_handle, ih.identity_id, i.display_name"
            " FROM identity_handles ih JOIN identities i ON i.id = ih.identity_id"
            " WHERE i.merged_into_id IS NULL ORDER BY ih.id DESC LIMIT $1",
            CANDIDATE_POOL,
        )
        candidates: list[tuple[float, int, PersonRef]] = []
        for row in rows:
            ref = PersonRef(row["platform"], row["raw_handle"], row["display_name"])
            score = candidate_score(new, ref)
            if score >= CANDIDATE_FLOOR:
                candidates.append((score, row["identity_id"], ref))
        candidates.sort(key=lambda c: c[0], reverse=True)

        for score, identity_id, ref in candidates[:MAX_LLM_CANDIDATES]:
            try:
                confidence = await self._judge.confirm(new, ref)
            except Exception as exc:  # judge failure must not crash resolution
                log.warning("same-person judge failed: %s — treating as 0", exc)
                continue
            if confidence >= CONFIRM_THRESHOLD:
                await self._link(
                    identity_id, platform, handle, normalized,
                    how=f"fuzzy candidate {ref.handle!r} (score {score:.0f}), LLM confidence {confidence:.2f}",
                )
                entity = await self.get(identity_id)
                assert entity is not None
                return entity

        return await self._create(platform, handle, normalized, display_name)

    async def merge(self, primary_id: int, duplicate_id: int) -> None:
        """Fold a duplicate identity into a primary one; recorded, never silent."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE identity_handles SET identity_id = $1 WHERE identity_id = $2",
                    primary_id,
                    duplicate_id,
                )
                await conn.execute(
                    "UPDATE identities SET merged_into_id = $1, confidence = 0"
                    " WHERE id = $2 AND merged_into_id IS NULL",
                    primary_id,
                    duplicate_id,
                )
        await self._audit.append(
            actor="agent",
            tool_name="entity_merge",
            decision="info",
            params={"primary": primary_id, "duplicate": duplicate_id},
            outcome="identities merged",
        )

    # -- internals -----------------------------------------------------------

    async def _by_normalized(self, normalized: str) -> Entity | None:
        row = await self._pool.fetchrow(
            "SELECT ih.identity_id, i.display_name, i.confidence"
            " FROM identity_handles ih"
            " JOIN identities i ON i.id = ih.identity_id"
            " WHERE ih.normalized = $1 AND i.merged_into_id IS NULL",
            normalized,
        )
        if row is None:
            return None
        handles = await self._pool.fetch(
            "SELECT platform, raw_handle FROM identity_handles WHERE identity_id = $1",
            row["identity_id"],
        )
        return Entity(
            id=row["identity_id"],
            display_name=row["display_name"],
            confidence=row["confidence"],
            handles=[(h["platform"], h["raw_handle"]) for h in handles],
        )

    async def _link(
        self, identity_id: int, platform: str, handle: str, normalized: str, *, how: str
    ) -> None:
        await self._pool.execute(
            "INSERT INTO identity_handles (identity_id, platform, raw_handle, normalized)"
            " VALUES ($1, $2, $3, $4) ON CONFLICT (platform, normalized) DO NOTHING",
            identity_id,
            platform,
            handle,
            normalized,
        )
        await self._audit.append(
            actor="agent",
            tool_name="entity_resolve",
            decision="info",
            params={"identity_id": identity_id, "handle": handle, "platform": platform},
            outcome=f"handle linked: {how}",
        )

    async def _create(
        self, platform: str, handle: str, normalized: str, display_name: str
    ) -> Entity:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                identity_id = await conn.fetchval(
                    "INSERT INTO identities (display_name) VALUES ($1) RETURNING id",
                    display_name or handle,
                )
                await conn.execute(
                    "INSERT INTO identity_handles (identity_id, platform, raw_handle, normalized)"
                    " VALUES ($1, $2, $3, $4) ON CONFLICT (platform, normalized) DO NOTHING",
                    identity_id,
                    platform,
                    handle,
                    normalized,
                )
        await self._audit.append(
            actor="agent",
            tool_name="entity_resolve",
            decision="info",
            params={"identity_id": identity_id, "handle": handle, "platform": platform},
            outcome="new identity created",
        )
        return Entity(
            id=identity_id,
            display_name=display_name or handle,
            confidence=1.0,
            handles=[(platform, handle)],
        )
