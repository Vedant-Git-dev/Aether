"""The facts layer: durable claims Aether holds, with provenance.

A fact is a small, typed claim about the world — the user's timezone, a
standing rule, a preference, a value observed in a message. It is what "what's
my X" and "how do I like Y" answer from, and it is what the core-facts block
puts in front of the model.

Everything the user wrote or received is sealed here. The row keeps only a
subject identity id, a keyed blind token over (subject, key), the status word
and storage bookkeeping in plaintext; the key, value, valid window, provenance
and confidence live in the sealed body. Equality lookup goes through the token,
so Postgres never reads a plaintext claim.

Consolidation is deterministic and is the whole point of the layer: a candidate
fact is compared with the active claims for its (subject, key) and becomes one
of ADD, IGNORE, SUPERSEDE or DISPUTED. A superseded claim is never overwritten —
its ``valid_until`` is set and its status changes, so the history stays. A fact
arriving from untrusted content cannot establish a reserved claim (identity,
contacts, policy, watches); it is held disputed and asked about once, on a
human turn.

The store is read whole-active into memory: facts are curated and few, so the
core-facts block ranks them in process rather than asking Postgres to sort a
column it is not allowed to read.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from .crypto import Cipher

log = logging.getLogger("aether.memory.facts")

# status vocabulary (plaintext)
ACTIVE = "active"
SUPERSEDED = "superseded"
DISPUTED = "disputed"

# consolidation outcomes
ADDED = "added"
IGNORED = "ignored"
SUPERSEDES = "superseded"
DISPUTES = "disputed"
REFUSED = "refused"

# the four shapes a claim can take. A durable claim the user stated (a
# preference, rule, identity, contact or policy) is never silently overwritten
# by a conflicting external claim.
FACT = "fact"
PREFERENCE = "preference"
RULE = "rule"
IDENTITY = "identity"
CONTACT = "contact"
POLICY = "policy"

TYPES = (FACT, PREFERENCE, RULE, IDENTITY, CONTACT, POLICY)
DURABLE_TYPES = (PREFERENCE, RULE, IDENTITY, CONTACT, POLICY)

# The claim domains untrusted content may not establish on its own: who the
# user is, how to reach them, what Aether may do, and what it should watch for.
# A candidate naming one of these from a non-owner source is held disputed and
# asked about once instead of becoming active.
_RESERVED_WORDS = frozenset(
    {
        "identity",
        "name",
        "email",
        "address",
        "phone",
        "contact",
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api",
        "policy",
        "permission",
        "watch",
    }
)
_RESERVED_RE = re.compile(
    r"[^a-z0-9]*(?:" + "|".join(_RESERVED_WORDS) + r")s?[^a-z0-9]*",
    re.IGNORECASE,
)
_VALUE_CAP = 500
_KEY_CAP = 80

# the question asked once about a disputed fact
_QUESTION = (
    "ask once, in your own words, whether {claim} is right — you could not "
    "confirm it from the user directly, so it stays unusable until they say so"
)


def _aad(fact_id: int) -> str:
    return f"facts:body_enc:{fact_id}"


def normalize_key(key: str) -> str:
    """The equality key: lowercased, collapsed, bounded."""
    return " ".join((key or "").split()).lower()[:_KEY_CAP]


def normalize_value(value: str) -> str:
    """The comparison form of a value — not what is stored (the original
    words are kept), only what decides whether two claims agree."""
    return " ".join((value or "").split()).lower()


def is_reserved_key(key: str) -> bool:
    """True when a claim names a domain untrusted content may not establish."""
    return bool(_RESERVED_RE.search(normalize_key(key)))


@dataclass(frozen=True)
class Fact:
    id: int
    subject_identity_id: int | None
    subject: str
    key: str
    value: str
    type: str
    status: str
    valid_from: datetime
    valid_until: datetime | None
    provenance: tuple[int, ...]
    confidence: float
    trusted: bool
    created_at: datetime
    asked: bool = False

    def is_valid(self, now: datetime) -> bool:
        if self.status != ACTIVE:
            return False
        if self.valid_from > now:
            return False
        return self.valid_until is None or self.valid_until > now


@dataclass
class Consolidation:
    """What one candidate fact did."""

    outcome: str
    fact: Fact | None = None

    @property
    def stored(self) -> bool:
        return self.fact is not None


class Facts:
    """The sealed store of durable claims, with deterministic consolidation."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher,
        audit: AuditLog | None = None,
        *,
        max_active: int = 500,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._max_active = max_active
        # one writer at a time, so a read-modify-write consolidation cannot
        # race another enrichment task into two active claims for one key
        self._lock = asyncio.Lock()

    def _token(self, subject_identity_id: int | None, subject: str, key: str) -> bytes:
        """The equality key: the resolved identity when there is one, and the
        normalized subject label otherwise, so two people's same-named key
        never collide."""
        label = " ".join((subject or "").split()).lower()
        return self._cipher.blind_token(
            "fact_key", f"{subject_identity_id or 0}\x1f{label}\x1f{key}"
        )

    # -- consolidation ------------------------------------------------------

    async def consolidate(
        self,
        *,
        subject_identity_id: int | None,
        key: str,
        value: str,
        subject: str = "",
        type: str = FACT,
        provenance: list[int] | tuple[int, ...] = (),
        trusted: bool = True,
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        actor: str = "system",
        now: datetime | None = None,
    ) -> Consolidation:
        """Compare one candidate claim with the active facts for its key.

        Provenance is mandatory: a claim that cannot name where it came from
        is refused rather than stored as an orphan belief.
        """
        now = now or datetime.now(UTC)
        clean_key = normalize_key(key)
        clean_value = (value or "").strip()[:_VALUE_CAP]
        value_key = normalize_value(clean_value)
        clean_subject = " ".join((subject or "").split())[:80]
        if not clean_key or not clean_value:
            return Consolidation(REFUSED)
        if not provenance:
            return Consolidation(REFUSED)
        raw_type = str(type or FACT).strip().lower()
        claim_type = raw_type if raw_type in TYPES else FACT
        token = self._token(subject_identity_id, clean_subject, clean_key)
        provenance_ids = tuple(dict.fromkeys(int(event_id) for event_id in provenance))
        reserved = is_reserved_key(clean_key)

        async with self._lock:
            existing = await self._matching(token)
            active = [fact for fact in existing if fact.status == ACTIVE]
            # an untrusted source may not establish a reserved claim
            if reserved and not trusted:
                fact = await self._insert(
                    subject_identity_id,
                    clean_subject,
                    clean_key,
                    clean_value,
                    claim_type,
                    DISPUTED,
                    provenance_ids,
                    trusted,
                    confidence,
                    valid_from or now,
                    None,
                )
                await self._audit_outcome(
                    actor, DISPUTES, fact, "an untrusted claim was held disputed"
                )
                return Consolidation(DISPUTES, fact)

            same = [fact for fact in active if normalize_value(fact.value) == value_key]
            if same:
                return Consolidation(IGNORED, same[0])
            same_disputed = [
                fact
                for fact in existing
                if fact.status == DISPUTED and normalize_value(fact.value) == value_key
            ]
            if same_disputed:
                return Consolidation(IGNORED, same_disputed[0])

            if active and not trusted:
                # external content that contradicts what is already known is
                # never silently applied — it is asked about once
                fact = await self._insert(
                    subject_identity_id,
                    clean_subject,
                    clean_key,
                    clean_value,
                    claim_type,
                    DISPUTED,
                    provenance_ids,
                    trusted,
                    confidence,
                    valid_from or now,
                    None,
                )
                await self._audit_outcome(actor, DISPUTES, fact, "an external claim conflicted")
                return Consolidation(DISPUTES, fact)

            if active:
                await self._supersede(active, now)
            fact = await self._insert(
                subject_identity_id,
                clean_subject,
                clean_key,
                clean_value,
                claim_type,
                ACTIVE,
                provenance_ids,
                trusted,
                confidence,
                valid_from or now,
                None,
            )
            if fact is None:
                return Consolidation(REFUSED)
            outcome = SUPERSEDES if active else ADDED
            await self._audit_outcome(actor, outcome, fact, "")
            return Consolidation(outcome, fact)

    async def _matching(self, token: bytes) -> list[Fact]:
        rows = await self._pool.fetch(
            "SELECT id, subject_identity_id, status, body_enc, created_at FROM facts"
            " WHERE key_token = $1 ORDER BY id DESC",
            token,
        )
        out: list[Fact] = []
        for row in rows:
            fact = self._to_fact(row)
            if fact is not None:
                out.append(fact)
        return out

    async def _insert(
        self,
        subject_identity_id: int | None,
        subject: str,
        key: str,
        value: str,
        claim_type: str,
        status: str,
        provenance: tuple[int, ...],
        trusted: bool,
        confidence: float,
        valid_from: datetime,
        valid_until: datetime | None,
    ) -> Fact | None:
        body = {
            "subject": subject,
            "key": key,
            "value": value,
            "type": claim_type,
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat() if valid_until else None,
            "provenance": list(provenance),
            "confidence": max(0.0, min(1.0, float(confidence))),
            "trusted": bool(trusted),
            "asked": False,
        }
        token = self._token(subject_identity_id, subject, key)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO facts (subject_identity_id, key_token, status,"
                    " body_enc, key_version) VALUES ($1, $2, $3, $4, $5)"
                    " RETURNING id, created_at",
                    subject_identity_id,
                    token,
                    status,
                    b"",  # placeholder until the id exists; replaced below, same transaction
                    self._cipher.key_id,
                )
                fact_id = row["id"]
                await conn.execute(
                    "UPDATE facts SET body_enc = $1 WHERE id = $2",
                    self._cipher.encrypt_json(body, aad=_aad(fact_id)),
                    fact_id,
                )
        except asyncpg.UniqueViolationError:
            # a concurrent writer won the active slot; treat this candidate as
            # already represented rather than failing the enrichment task
            log.info("a fact for one key was already active — candidate dropped")
            return None
        return Fact(
            id=fact_id,
            subject_identity_id=subject_identity_id,
            subject=subject,
            key=key,
            value=value,
            type=claim_type,
            status=status,
            valid_from=valid_from,
            valid_until=valid_until,
            provenance=provenance,
            confidence=body["confidence"],
            trusted=trusted,
            created_at=row["created_at"],
        )

    async def _supersede(self, facts: list[Fact], now: datetime) -> None:
        """End the active claims without touching their words — the row keeps
        what was believed and gains an end of validity."""
        for fact in facts:
            await self._pool.execute(
                "UPDATE facts SET status = $1, body_enc = $2 WHERE id = $3 AND status = $4",
                SUPERSEDED,
                self._cipher.encrypt_json(
                    {
                        "subject": fact.subject,
                        "key": fact.key,
                        "value": fact.value,
                        "type": fact.type,
                        "valid_from": fact.valid_from.isoformat(),
                        "valid_until": now.isoformat(),
                        "provenance": list(fact.provenance),
                        "confidence": fact.confidence,
                        "trusted": fact.trusted,
                        "asked": fact.asked,
                    },
                    aad=_aad(fact.id),
                ),
                fact.id,
                ACTIVE,
            )

    async def _audit_outcome(self, actor: str, outcome: str, fact: Fact | None, note: str) -> None:
        if self._audit is None:
            return
        await self._audit.append(
            actor=actor,
            tool_name="fact",
            decision="info",
            params={
                "outcome": outcome,
                "fact_type": fact.type if fact is not None else "",
                "fact_id": fact.id if fact is not None else 0,
            },
            outcome=note or f"fact {outcome}",
        )

    # -- reads --------------------------------------------------------------

    async def active(self, now: datetime | None = None, limit: int | None = None) -> list[Fact]:
        """Every active fact valid now — the core-facts block's own read."""
        now = now or datetime.now(UTC)
        rows = await self._pool.fetch(
            "SELECT id, subject_identity_id, status, body_enc, created_at FROM facts"
            " WHERE status = $1 ORDER BY id DESC LIMIT $2",
            ACTIVE,
            limit or self._max_active,
        )
        facts = [fact for row in rows if (fact := self._to_fact(row)) is not None]
        return [fact for fact in facts if fact.is_valid(now)]

    async def for_subject(
        self, subject_identity_id: int, *, now: datetime | None = None, limit: int = 50
    ) -> list[Fact]:
        now = now or datetime.now(UTC)
        rows = await self._pool.fetch(
            "SELECT id, subject_identity_id, status, body_enc, created_at FROM facts"
            " WHERE subject_identity_id = $1 AND status = $2 ORDER BY id DESC LIMIT $3",
            subject_identity_id,
            ACTIVE,
            limit,
        )
        facts = [fact for row in rows if (fact := self._to_fact(row)) is not None]
        return [fact for fact in facts if fact.is_valid(now)]

    async def get(self, fact_id: int) -> Fact | None:
        row = await self._pool.fetchrow(
            "SELECT id, subject_identity_id, status, body_enc, created_at FROM facts WHERE id = $1",
            fact_id,
        )
        return self._to_fact(row) if row else None

    # -- the ask-once exchange ----------------------------------------------

    async def pending_questions(self, limit: int = 3, now: datetime | None = None) -> list[Fact]:
        """Disputed claims surfaced once, on a human turn only."""
        now = now or datetime.now(UTC)
        rows = await self._pool.fetch(
            "SELECT id, subject_identity_id, status, body_enc, created_at FROM facts"
            " WHERE status = $1 ORDER BY id DESC LIMIT 200",
            DISPUTED,
        )
        found = [fact for row in rows if (fact := self._to_fact(row)) is not None]
        pending = [fact for fact in found if not fact.asked]
        pending.sort(key=lambda fact: fact.created_at, reverse=True)
        return pending[:limit]

    async def mark_asked(self, fact_ids: list[int]) -> None:
        """Record that the one question was put to the user, so it is not
        repeated — the marker rides the sealed body like every other field."""
        for fact_id in fact_ids:
            fact = await self.get(fact_id)
            if fact is None or fact.asked:
                continue
            await self._pool.execute(
                "UPDATE facts SET body_enc = $1 WHERE id = $2",
                self._cipher.encrypt_json(
                    {
                        "subject": fact.subject,
                        "key": fact.key,
                        "value": fact.value,
                        "type": fact.type,
                        "valid_from": fact.valid_from.isoformat(),
                        "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
                        "provenance": list(fact.provenance),
                        "confidence": fact.confidence,
                        "trusted": fact.trusted,
                        "asked": True,
                    },
                    aad=_aad(fact.id),
                ),
                fact_id,
            )

    def question_text(self, fact: Fact) -> str:
        """One disputed claim as the words the model should use."""
        claim = f'"{fact.value}" as {fact.key}'
        return _QUESTION.format(claim=claim)

    # -- internals ----------------------------------------------------------

    def _to_fact(self, row: asyncpg.Record) -> Fact | None:
        fact_id = int(row["id"])
        try:
            body = self._cipher.decrypt_json(row["body_enc"], aad=_aad(fact_id))
        except Exception:
            log.warning("fact %s body undecryptable — skipped", fact_id)
            return None
        if not isinstance(body, dict):
            return None
        return Fact(
            id=fact_id,
            subject_identity_id=row["subject_identity_id"],
            subject=str(body.get("subject") or ""),
            key=str(body.get("key") or ""),
            value=str(body.get("value") or ""),
            type=str(body.get("type") or FACT),
            status=row["status"],
            valid_from=_optional_time(body.get("valid_from")) or row["created_at"],
            valid_until=_optional_time(body.get("valid_until")),
            provenance=tuple(int(item) for item in (body.get("provenance") or []) if _is_int(item)),
            confidence=_as_float(body.get("confidence"), 1.0),
            trusted=bool(body.get("trusted", True)),
            created_at=row["created_at"],
            asked=bool(body.get("asked", False)),
        )


def _optional_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _as_float(value: Any, fallback: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback
