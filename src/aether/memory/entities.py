"""Cross-platform identity resolution and relationship notes.

The same person shows up as an email address, a Telegram handle, a Discord
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

# entity-note kinds for the §4.2 alias exchange. The question is asked
# once; the confirmation is what turns an address into one of the owner's
# own, at the moment it is recorded.
ALIAS_QUESTION = "alias_question"
ALIAS_CONFIRMED = "owner_alias"


# ---------------------------------------------------------------------------
# handle normalization (shared with the contact allowlist in events.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sender:
    platform: str
    handle: str


_PHONE_PLATFORMS = {"phone", "sms", "call"}


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
    is_phone = platform in _PHONE_PLATFORMS or (h.startswith("+") and len(digits) >= 7)
    if is_phone:
        key = digits[-10:] if len(digits) >= 10 else digits
        return f"tel:{key}"
    if "@" in h and not h.startswith("@"):
        return f"email:{h}"
    return f"{platform}:{h.lstrip('@')}"


def _identity_profile(cipher: Cipher, row: Any) -> tuple[str, float]:
    """Name and confidence from the sealed profile."""
    identity_id = row.get("id", row.get("identity_id"))
    return _profile_for(cipher, identity_id, row.get("profile_enc"))


def _profile_for(cipher: Cipher, identity_id: int, blob: Any) -> tuple[str, float]:
    profile = _profile_data(cipher, identity_id, blob)
    return str(profile.get("display_name", "")), float(profile.get("confidence", 1.0))


def _profile_data(cipher: Cipher, identity_id: int, blob: Any) -> dict[str, Any]:
    """The whole sealed profile — name, confidence, and whether this
    identity is one of the owner's own addresses (plan §8)."""
    if not blob:
        return {"display_name": "", "confidence": 1.0, "owner": False}
    data = cipher.decrypt_json(blob, aad=f"identities:profile_enc:{identity_id}")
    if not isinstance(data, dict):
        return {"display_name": "", "confidence": 1.0, "owner": False}
    return data


def _is_owner(profile: dict[str, Any]) -> bool:
    return bool(profile.get("owner", False))


def _handle_ref(cipher: Cipher, row: Any) -> tuple[str, str]:
    """(platform, raw_handle) for one identity_handles row."""
    blob = row.get("handle_enc")
    if not blob:
        return "", ""
    handle_id = row.get("id", row.get("handle_id"))
    data = cipher.decrypt_json(blob, aad=f"identity_handles:handle_enc:{handle_id}")
    return str(data.get("platform", "")), str(data.get("handle", ""))


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
    # one of the owner's own addresses — rendered as "(your other address)"
    # and never collapsed into the primary, per plan §8
    owner: bool = False


@dataclass(frozen=True)
class LinkedAddress:
    """One address from an event, linked to an identity.

    `alias_candidate` is the §4.2 case: the display name looks like the
    owner's but the address is new. It is reported, never acted on — the
    sender is never merged into the owner's identity on a guess.
    """

    entity: Entity
    alias_candidate: bool = False
    alias_owner_id: int | None = None


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
        if self._provider is None:
            # resolve() treats judge failure as "not the same person" — an
            # agent running without LLM keys just gets more identities
            raise RuntimeError("no LLM provider configured")
        payload = {
            "reference_a": {
                "platform": a.platform,
                "handle": a.handle,
                "display_name": a.display_name,
            },
            "reference_b": {
                "platform": b.platform,
                "handle": b.handle,
                "display_name": b.display_name,
            },
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
        scores.append(fuzz.token_set_ratio(new.display_name.lower(), existing.display_name.lower()))
    new_local, _, _ = new.handle.partition("@")
    old_local, _, _ = existing.handle.partition("@")
    if new_local and old_local and "@" in new.handle and "@" in existing.handle:
        scores.append(fuzz.ratio(new_local.lower(), old_local.lower()))
    new_digits = re.sub(r"\D", "", new.handle)
    old_digits = re.sub(r"\D", "", existing.handle)
    if len(new_digits) >= 7 and len(old_digits) >= 7:
        scores.append(
            100.0
            if new_digits[-10:] == old_digits[-10:]
            else float(fuzz.ratio(new_digits, old_digits))
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
        # the owner's own addresses, cached — consulted for every rendered
        # address and invalidated the moment one is marked
        self._owners: set[str] | None = None
        self._owner_cache: list[Entity] | None = None

    # -- reads ------------------------------------------------------------

    async def resolve_exact(self, platform: str, handle: str) -> Entity | None:
        normalized = normalize_handle(platform, handle)
        if not normalized:
            return None
        return await self._by_normalized(normalized)

    async def get(self, identity_id: int) -> Entity | None:
        row = await self._pool.fetchrow(
            "SELECT id, profile_enc FROM identities WHERE id = $1 AND merged_into_id IS NULL",
            identity_id,
        )
        if row is None:
            return None
        profile = _profile_data(self._cipher, row["id"], row.get("profile_enc"))
        handles = await self._pool.fetch(
            "SELECT id, handle_enc FROM identity_handles WHERE identity_id = $1",
            identity_id,
        )
        return Entity(
            id=row["id"],
            display_name=str(profile.get("display_name", "")),
            confidence=float(profile.get("confidence", 1.0)),
            handles=[_handle_ref(self._cipher, h) for h in handles],
            owner=_is_owner(profile),
        )

    async def list_people(self, limit: int = 50) -> list[Entity]:
        """Everyone Aether has resolved an identity for, most recent first —
        the read side of the "who is this" cross-platform linking."""
        rows = await self._pool.fetch(
            "SELECT id, profile_enc FROM identities"
            " WHERE merged_into_id IS NULL ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        handle_rows = await self._pool.fetch(
            "SELECT id, identity_id, handle_enc FROM identity_handles"
            " WHERE identity_id = ANY($1::bigint[])",
            ids,
        )
        handles_by_id: dict[int, list[tuple[str, str]]] = {}
        for h in handle_rows:
            handles_by_id.setdefault(h["identity_id"], []).append(_handle_ref(self._cipher, h))
        people: list[Entity] = []
        for r in rows:
            profile = _profile_data(self._cipher, r["id"], r.get("profile_enc"))
            people.append(
                Entity(
                    id=r["id"],
                    display_name=str(profile.get("display_name", "")),
                    confidence=float(profile.get("confidence", 1.0)),
                    handles=handles_by_id.get(r["id"], []),
                    owner=_is_owner(profile),
                )
            )
        return people

    async def note(self, identity_id: int, payload: dict[str, Any]) -> None:
        """Append an encrypted relationship note for an identity.

        One note kind has a side effect: the owner-alias confirmation. When
        the user answers the §4.2 question, the address becomes one of the
        owner's own at that moment — otherwise the answer would sit in a
        note and mean nothing until some later pass noticed it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            note_id = await conn.fetchval(
                "INSERT INTO entity_notes (identity_id, payload_enc) VALUES ($1, $2) RETURNING id",
                identity_id,
                b"",  # placeholder until the id exists; replaced below, same transaction
            )
            blob = self._cipher.encrypt_json(payload, aad=f"entity_notes:payload_enc:{note_id}")
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
        if str(payload.get("kind", "")) == ALIAS_CONFIRMED:
            await self.mark_owner(identity_id)

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

    async def resolve(self, platform: str, handle: str, display_name: str = "") -> Entity:
        """Link a reference to an identity — exact match, fuzzy candidates
        confirmed by the LLM, or a fresh identity."""
        normalized = normalize_handle(platform, handle)
        exact = await self._by_normalized(normalized)
        if exact is not None:
            return exact

        new = PersonRef(platform, handle, display_name)
        rows = await self._pool.fetch(
            "SELECT ih.id AS handle_id, ih.handle_enc, ih.identity_id, i.profile_enc"
            " FROM identity_handles ih JOIN identities i ON i.id = ih.identity_id"
            " WHERE i.merged_into_id IS NULL ORDER BY ih.id DESC LIMIT $1",
            CANDIDATE_POOL,
        )
        candidates: list[tuple[float, int, PersonRef]] = []
        for row in rows:
            handle_platform, raw_handle = _handle_ref(self._cipher, row)
            name, _ = _profile_for(self._cipher, row["identity_id"], row["profile_enc"])
            ref = PersonRef(handle_platform, raw_handle, name)
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
                    identity_id,
                    platform,
                    handle,
                    normalized,
                    how=f"fuzzy candidate {ref.handle!r} (score {score:.0f}), LLM confidence {confidence:.2f}",
                )
                entity = await self.get(identity_id)
                assert entity is not None
                return entity

        return await self._create(platform, handle, normalized, display_name)

    async def merge(self, primary_id: int, duplicate_id: int) -> None:
        """Fold a duplicate identity into a primary one; recorded, never silent."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE identity_handles SET identity_id = $1 WHERE identity_id = $2",
                primary_id,
                duplicate_id,
            )
            await conn.execute(
                "UPDATE identities SET merged_into_id = $1"
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
        token = self._cipher.blind_token("handle", normalized)
        row = await self._pool.fetchrow(
            "SELECT ih.identity_id, i.profile_enc"
            " FROM identity_handles ih"
            " JOIN identities i ON i.id = ih.identity_id"
            " WHERE ih.normalized_token = $1 AND i.merged_into_id IS NULL",
            token,
        )
        if row is None:
            return None
        handles = await self._pool.fetch(
            "SELECT id, handle_enc FROM identity_handles WHERE identity_id = $1",
            row["identity_id"],
        )
        profile = _profile_data(self._cipher, row["identity_id"], row.get("profile_enc"))
        return Entity(
            id=row["identity_id"],
            display_name=str(profile.get("display_name", "")),
            confidence=float(profile.get("confidence", 1.0)),
            handles=[_handle_ref(self._cipher, h) for h in handles],
            owner=_is_owner(profile),
        )

    async def _link(
        self, identity_id: int, platform: str, handle: str, normalized: str, *, how: str
    ) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO identity_handles (identity_id, platform_token,"
                " normalized_token, handle_enc, key_version)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (platform_token, normalized_token)"
                " WHERE platform_token IS NOT NULL AND normalized_token IS NOT NULL"
                " DO NOTHING RETURNING id",
                identity_id,
                self._cipher.blind_token("platform", platform),
                self._cipher.blind_token("handle", normalized),
                b"",
                self._cipher.key_id,
            )
            if row is not None:
                handle_id = row["id"]
                await conn.execute(
                    "UPDATE identity_handles SET handle_enc = $1 WHERE id = $2",
                    self._cipher.encrypt_json(
                        {"platform": platform, "handle": handle},
                        aad=f"identity_handles:handle_enc:{handle_id}",
                    ),
                    handle_id,
                )
        await self._audit.append(
            actor="agent",
            tool_name="entity_resolve",
            decision="info",
            params={"identity_id": identity_id, "handle": handle, "platform": platform},
            outcome="handle linked",
        )

    async def _create(
        self, platform: str, handle: str, normalized: str, display_name: str
    ) -> Entity:
        async with self._pool.acquire() as conn, conn.transaction():
            identity_id = await conn.fetchval(
                "INSERT INTO identities (profile_enc) VALUES ($1) RETURNING id",
                b"",  # placeholder until the id exists; replaced below, same transaction
            )
            await conn.execute(
                "UPDATE identities SET profile_enc = $1, key_version = $2 WHERE id = $3",
                self._cipher.encrypt_json(
                    {"display_name": display_name or handle, "confidence": 1.0},
                    aad=f"identities:profile_enc:{identity_id}",
                ),
                self._cipher.key_id,
                identity_id,
            )
            handle_row = await conn.fetchrow(
                "INSERT INTO identity_handles (identity_id, platform_token,"
                " normalized_token, handle_enc, key_version)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (platform_token, normalized_token)"
                " WHERE platform_token IS NOT NULL AND normalized_token IS NOT NULL"
                " DO NOTHING RETURNING id",
                identity_id,
                self._cipher.blind_token("platform", platform),
                self._cipher.blind_token("handle", normalized),
                b"",
                self._cipher.key_id,
            )
            if handle_row is not None:
                handle_id = handle_row["id"]
                await conn.execute(
                    "UPDATE identity_handles SET handle_enc = $1 WHERE id = $2",
                    self._cipher.encrypt_json(
                        {"platform": platform, "handle": handle},
                        aad=f"identity_handles:handle_enc:{handle_id}",
                    ),
                    handle_id,
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

    # -- write-time linking (plan §4.2) --------------------------------------

    async def link_address(
        self, platform: str, handle: str, display_name: str = ""
    ) -> LinkedAddress | None:
        """Link one address from an event, deterministically.

        An exact blind-index hit, else a fresh identity — no model call, no
        fuzzy merge. A mail arriving is not a reason to spend a turn
        deciding who somebody is, and the expensive, fallible identity work
        stays where a human asked for it (`resolve`).

        The §4.2 alias case is *reported*, never acted on: a display name
        that looks like one of the owner's own, on an address nobody has
        seen, comes back as a candidate so the caller can ask once. The
        sender is never folded into the owner's identity on a guess.
        """
        if not (handle or "").strip():
            return None
        existing = await self.resolve_exact(platform, handle)
        if existing is not None:
            return LinkedAddress(entity=existing)

        entity = await self._create(
            platform,
            handle,
            normalize_handle(platform, handle),
            display_name,
        )
        owner = await self._owner_matching_name(display_name)
        if owner is None:
            return LinkedAddress(entity=entity)
        return LinkedAddress(
            entity=entity,
            alias_candidate=True,
            alias_owner_id=owner.id,
        )

    async def mark_owner(self, identity_id: int) -> bool:
        """Record that this identity is one of the owner's own addresses.

        The flag rides the sealed profile (a plaintext column would be
        illegal for a user-derived address); it is what renders as
        "(your other address)" and what keeps the sender distinct from the
        owner's primary identity.
        """
        entity = await self.get(identity_id)
        if entity is None:
            return False
        if entity.owner:
            return True
        await self._write_profile(
            identity_id,
            display_name=entity.display_name,
            confidence=entity.confidence,
            owner=True,
        )
        # both caches answer questions about owners — a new one invalidates
        # the address set and the owner-entity list together
        self._owners = None
        self._owner_cache = None
        await self._audit.append(
            actor="system",
            tool_name="entity_owner",
            decision="info",
            params={"identity_id": identity_id},
            outcome="identity marked as one of the owner's addresses",
        )
        return True

    async def mark_owner_address(
        self, platform: str, handle: str, display_name: str = ""
    ) -> Entity | None:
        """Claim an address as the owner's own — the Gmail account address
        on connect, so the first mail already renders correctly."""
        linked = await self.link_address(platform, handle, display_name)
        if linked is None:
            return None
        await self.mark_owner(linked.entity.id)
        return await self.get(linked.entity.id)

    async def owner_addresses(self) -> set[str]:
        """The normalized handles of every identity marked as the owner's.

        Cached: it is consulted for every rendered address and changes only
        when the owner connects another account.
        """
        if self._owners is None:
            rows = await self._pool.fetch("SELECT id, profile_enc FROM identities")
            keys: set[str] = set()
            for row in rows:
                profile = _profile_data(self._cipher, row["id"], row.get("profile_enc"))
                if not _is_owner(profile):
                    continue
                handles = await self._pool.fetch(
                    "SELECT id, handle_enc FROM identity_handles WHERE identity_id = $1",
                    row["id"],
                )
                for handle_row in handles:
                    handle_platform, raw = _handle_ref(self._cipher, handle_row)
                    normalized = normalize_handle(handle_platform, raw)
                    if normalized:
                        keys.add(normalized)
            self._owners = keys
        return set(self._owners)

    async def is_owner_address(self, platform: str, handle: str) -> bool:
        normalized = normalize_handle(platform, handle)
        if not normalized:
            return False
        return normalized in await self.owner_addresses()

    # -- the ask-once alias question ----------------------------------------

    async def ask_alias_once(
        self, identity_id: int, *, address: str, name: str, owner_name: str
    ) -> bool:
        """Record the one question about a possible owner alias.

        Returns True the first time and False afterwards, so the user is
        asked once. It is stored as an `entity_note` — append-only, sealed,
        and read back by `notes_for`, which is what makes "once" true across
        restarts without a new table.
        """
        for note in await self.notes_for(identity_id, limit=50):
            if str(note.payload.get("kind", "")) == ALIAS_QUESTION:
                return False
        await self.note(
            identity_id,
            {"kind": ALIAS_QUESTION, "address": address, "name": name, "owner": owner_name},
        )
        return True

    async def pending_alias_questions(self, limit: int = 3) -> list[EntityNote]:
        """Unanswered alias questions, newest first.

        Surfaced on a human turn only — plan §6 forbids mail from prompting
        the user by itself, and this is a question, not an alert.
        """
        rows = await self._pool.fetch(
            "SELECT id, identity_id, created_at, payload_enc FROM entity_notes"
            " ORDER BY id DESC LIMIT 500"
        )
        owned = {person.id for person in await self._owner_entities()}
        asked: dict[int, EntityNote] = {}
        answered: set[int] = set(owned)
        for row in rows:
            try:
                payload = self._cipher.decrypt_json(
                    row["payload_enc"], aad=f"entity_notes:payload_enc:{row['id']}"
                )
            except Exception:
                continue
            kind = str(payload.get("kind", ""))
            identity_id = int(row["identity_id"])
            if kind == ALIAS_CONFIRMED:
                answered.add(identity_id)
            elif kind == ALIAS_QUESTION and identity_id not in asked:
                asked[identity_id] = EntityNote(
                    id=row["id"],
                    identity_id=identity_id,
                    created_at=row["created_at"],
                    payload=payload,
                )
        pending = [note for identity_id, note in asked.items() if identity_id not in answered]
        pending.sort(key=lambda note: note.created_at, reverse=True)
        return pending[:limit]

    async def _owner_matching_name(self, display_name: str) -> Entity | None:
        """An owner identity whose name looks like this one's, or None."""
        wanted = (display_name or "").strip().lower()
        if not wanted:
            return None
        for owner in await self._owner_entities():
            name = (owner.display_name or "").strip().lower()
            if name and fuzz.token_set_ratio(wanted, name) >= CANDIDATE_FLOOR:
                return owner
        return None

    async def _owner_entities(self) -> list[Entity]:
        if self._owner_cache is None:
            self._owner_cache = [
                person for person in await self.list_people(limit=CANDIDATE_POOL) if person.owner
            ]
        return self._owner_cache

    async def _write_profile(
        self, identity_id: int, *, display_name: str, confidence: float, owner: bool
    ) -> None:
        await self._pool.execute(
            "UPDATE identities SET profile_enc = $1, key_version = $2 WHERE id = $3",
            self._cipher.encrypt_json(
                {"display_name": display_name, "confidence": confidence, "owner": owner},
                aad=f"identities:profile_enc:{identity_id}",
            ),
            self._cipher.key_id,
            identity_id,
        )
