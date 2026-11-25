"""Append-only, hash-chained audit log.

    legacy entry:  entry_hash = sha256(canonical(entry))
    sealed entry:  entry_hash = sha256(prev_hash || payload_ciphertext)

Each row commits to the entire history before it, so any edit, deletion,
or reordering is detectable by re-walking the chain with verify_chain() —
the chain check is the tamper evidence, not the database's own grants.

A row written before the sealed format landed keeps its plaintext columns
and the legacy formula. A new row stores only the opaque chain metadata in
the clear (sequence, previous hash, entry hash, timestamp) and seals the
canonical entry — actor, tool, decision, rules, the parameter digest and
the outcome — as a payload whose ciphertext the hash commits to. The panel
reads both through `recent()`, which opens sealed payloads.

The first sealed entry after legacy rows is the transition: its sealed
payload records the legacy head hash and count, so the point where the
format changes is itself authenticated. Truncation of the tail is out of
the chain's reach; compare `ChainVerification.head_hash` and `entries`
against an independently retained checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..memory.crypto import CryptoError
from ..memory.rows import Rows, ScopedRows
from ..vault.schema import b64u_encode, canonical_bytes
from ..vault.scope import LegacyCiphertextError

GENESIS_HASH = "0" * 64


class AuditPayloadUnreadable(RuntimeError):
    """The protected audit payload could not be read."""


def params_digest(params: Mapping[str, Any]) -> str:
    """sha256 of the canonical parameter JSON — identifies the call without
    storing what was in it."""
    blob = json.dumps(
        dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_entry(
    *,
    seq: int,
    actor: str,
    tool_name: str,
    decision: str,
    rules_matched: str,
    params_digest: str,
    outcome: str,
    created_at: str,
    prev_hash: str,
) -> str:
    """The exact byte string every legacy entry hash commits to. Field order
    is pinned by sort_keys, so it never depends on how a caller built a
    dict."""
    payload = {
        "seq": seq,
        "actor": actor,
        "tool_name": tool_name,
        "decision": decision,
        "rules_matched": rules_matched,
        "params_digest": params_digest,
        "outcome": outcome,
        "created_at": created_at,
        "prev_hash": prev_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_entry_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_sealed_entry_hash(prev_hash: str, payload_enc: bytes) -> str:
    """The sealed chain hashes the opaque ciphertext, never the plaintext —
    verification needs no key, and the AEAD guards the contents."""
    return hashlib.sha256(prev_hash.encode("utf-8") + bytes(payload_enc)).hexdigest()


def _created_at_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    entries: int  # entries examined
    first_bad_seq: int | None = None
    problem: str | None = None
    head_hash: str | None = None  # the newest entry hash — compare off-site


def checkpoint_bytes(
    verification: ChainVerification, *, vault_id: bytes, job_id: bytes, archived: bool
) -> bytes:
    """Bytes for the operator's independent, offline audit receipt."""
    if not verification.ok:
        raise ValueError("cannot checkpoint a broken audit chain")
    return canonical_bytes(
        {
            "type": "aether_audit_checkpoint",
            "v": 1,
            "vault": b64u_encode(vault_id),
            "job": b64u_encode(job_id),
            "archived": archived,
            "entries": verification.entries,
            "head": verification.head_hash or GENESIS_HASH,
        }
    )


def matches_checkpoint(
    verification: ChainVerification,
    *,
    vault_id: bytes,
    job_id: bytes,
    archived: bool,
    signature: bytes,
    signer: Ed25519PublicKey,
) -> bool:
    if not verification.ok:
        return False
    try:
        signer.verify(
            signature,
            checkpoint_bytes(verification, vault_id=vault_id, job_id=job_id, archived=archived),
        )
    except InvalidSignature:
        return False
    return True


def legacy_canonical(row: Mapping[str, Any]) -> str:
    return canonical_entry(
        seq=row["seq"],
        actor=row["actor"],
        tool_name=row["tool_name"],
        decision=row["decision"],
        rules_matched=row["rules_matched"],
        params_digest=row["params_digest"],
        outcome=row["outcome"],
        created_at=_created_at_iso(row["created_at"]),
        prev_hash=row["prev_hash"],
    )


def verify_entries(rows: Iterable[Mapping[str, Any]]) -> ChainVerification:
    """Walk rows (in seq order) and recompute the chain. Works on plain
    dicts, asyncpg Records — anything with key access.

    A row with a payload verifies against its ciphertext; a legacy row
    verifies against its canonical plaintext fields. Both commit to
    `prev_hash`, so a chain that crosses the transition verifies as one.
    """
    prev = GENESIS_HASH
    count = 0
    head: str | None = None
    for row in rows:
        count += 1
        seq = row["seq"]
        if row["prev_hash"] != prev:
            return ChainVerification(
                False,
                count,
                seq,
                f"seq {seq}: prev_hash does not chain to the previous entry",
            )
        payload_enc = row.get("payload_enc")
        if payload_enc:
            expected = compute_sealed_entry_hash(row["prev_hash"], payload_enc)
        else:
            expected = compute_entry_hash(legacy_canonical(row))
        if expected != row["entry_hash"]:
            return ChainVerification(
                False, count, seq, f"seq {seq}: stored hash does not match the entry contents"
            )
        prev = row["entry_hash"]
        head = row["entry_hash"]
    return ChainVerification(True, count, head_hash=head)


def _age(now: datetime, then: datetime) -> str:
    """Coarse elapsed words, "moments ago" to "N days ago" — the badge's
    'last act' is a feeling, not a timestamp."""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)  # naive stamps are UTC here
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 60:
        return "moments ago"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minute{'s' if minutes >= 2 else ''} ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'s' if hours >= 2 else ''} ago"
    days = hours / 24
    return f"{int(days)} day{'s' if days >= 2 else ''} ago"


def verification_text(verification: ChainVerification, newest_at: datetime | None = None) -> str:
    """A chain verification as the user reads it in chat — the same facts
    the panel badge shows, in plain words. Pure: the caller reads the rows."""
    if verification.entries == 0:
        return "decision record: empty — nothing recorded yet."
    if verification.ok:
        last = f", last act {_age(datetime.now(UTC), newest_at)}" if newest_at is not None else ""
        plural = "s" if verification.entries != 1 else ""
        return (
            f"decision record: {verification.entries:,} decision{plural}, "
            f"chain intact — every entry still hashes to the one before it{last}."
        )
    where = f" at entry #{verification.first_bad_seq}" if verification.first_bad_seq else ""
    problem = f" — {verification.problem}" if verification.problem else ""
    return f"decision record: BROKEN{where}{problem}. Everything from there on can't be trusted."


_SELECT = (
    "SELECT seq, actor, tool_name, decision, rules_matched, params_digest,"
    " outcome, prev_hash, entry_hash, created_at, payload_enc FROM audit_log"
)


class AuditLog:
    """Database-backed chain writer/verifier.

    With a `rows` seam every new entry is sealed and the chain hashes its
    ciphertext. Without one the legacy plaintext format is used, which is
    what existing tests and tooling drive.
    """

    def __init__(self, pool: asyncpg.Pool, rows: Rows | None = None) -> None:
        self._pool = pool
        self._rows = rows

    @classmethod
    def scoped(cls, pool: asyncpg.Pool, scope: Any, *, ver: int = 1) -> AuditLog:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, ScopedRows(scope, cat="operations", ver=ver))

    async def append(
        self,
        *,
        actor: str,
        tool_name: str,
        decision: str,
        rules_matched: str = "",
        params: Mapping[str, Any] | None = None,
        outcome: str = "",
    ) -> int:
        if self._rows is not None:
            # fail before touching the pool when the task has no scope
            self._rows.preflight()
        created_at = datetime.now(UTC)
        digest = params_digest(params or {})
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE audit_log IN SHARE ROW EXCLUSIVE MODE")
            if self._rows is not None:
                self._rows.preflight()
            seq = await conn.fetchval("SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_log")
            prev = await conn.fetchrow(
                "SELECT entry_hash, payload_enc FROM audit_log ORDER BY seq DESC LIMIT 1"
            )
            prev_hash = (prev["entry_hash"] if prev is not None else None) or GENESIS_HASH
            if self._rows is None:
                entry_hash = compute_entry_hash(
                    canonical_entry(
                        seq=seq,
                        actor=actor,
                        tool_name=tool_name,
                        decision=decision,
                        rules_matched=rules_matched,
                        params_digest=digest,
                        outcome=outcome,
                        created_at=created_at.isoformat(),
                        prev_hash=prev_hash,
                    )
                )
                await conn.execute(
                    "INSERT INTO audit_log (seq, actor, tool_name, decision,"
                    " rules_matched, params_digest, outcome, prev_hash,"
                    " entry_hash, created_at)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    seq,
                    actor,
                    tool_name,
                    decision,
                    rules_matched,
                    digest,
                    outcome,
                    prev_hash,
                    entry_hash,
                    created_at,
                )
                return seq
            payload: dict[str, Any] = {
                "seq": seq,
                "actor": actor,
                "tool_name": tool_name,
                "decision": decision,
                "rules_matched": rules_matched,
                "params_digest": digest,
                "outcome": outcome,
                "created_at": created_at.isoformat(),
                "prev_hash": prev_hash,
            }
            if prev is None or prev["payload_enc"] is None:
                # the transition entry: anchor the legacy head and count so
                # the format change is authenticated in the sealed chain
                legacy_count = await conn.fetchval(
                    "SELECT count(*) FROM audit_log WHERE payload_enc IS NULL"
                )
                payload["legacy_head"] = prev_hash if prev is not None else GENESIS_HASH
                payload["legacy_count"] = int(legacy_count or 0)
            payload_enc = self._rows.seal(
                payload,
                table="audit_log",
                column="payload_enc",
                row_id=seq,
                aad=f"audit_log:payload_enc:{seq}",
            )
            entry_hash = compute_sealed_entry_hash(prev_hash, payload_enc)
            await conn.execute(
                "INSERT INTO audit_log (seq, actor, tool_name, decision,"
                " rules_matched, params_digest, outcome, prev_hash,"
                " entry_hash, created_at, payload_enc)"
                " VALUES ($1, '', '', '', '', '', '', $2, $3, $4, $5)",
                seq,
                prev_hash,
                entry_hash,
                created_at,
                payload_enc,
            )
        return seq

    async def verify_chain(self) -> ChainVerification:
        """Verify the whole chain — the tamper evidence the panel shows.

        This is deliberately a full walk: an incremental check would miss
        an edit to an old row while the process is running. Sealed rows
        verify from their ciphertext, so no key is needed to check the
        chain and a bit flip or reorder fails here.
        """
        rows = await self._pool.fetch(_SELECT + " ORDER BY seq")
        return verify_entries(rows)

    async def recent(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Newest first, sealed payloads opened — the panel and /verify view.

        Legacy rows come back from their plaintext columns; sealed rows are
        opened through the row seam, so the shape is identical for both.
        """
        if self._rows is not None:
            self._rows.preflight()
        rows = await self._pool.fetch(
            _SELECT + " ORDER BY seq DESC LIMIT $1 OFFSET $2",
            limit,
            max(0, offset),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            payload_enc = row.get("payload_enc")
            if not payload_enc:
                out.append(dict(row))
                continue
            if self._rows is None:
                raise AuditPayloadUnreadable("An audit entry could not be read.")
            self._rows.preflight()
            try:
                data = self._rows.open(
                    payload_enc,
                    table="audit_log",
                    column="payload_enc",
                    row_id=row["seq"],
                    aad=f"audit_log:payload_enc:{row['seq']}",
                )
                if not isinstance(data, dict):
                    raise ValueError("audit payload is not an object")
            except (CryptoError, LegacyCiphertextError, ValueError) as exc:
                raise AuditPayloadUnreadable("An audit entry could not be read.") from exc
            merged = dict(row)
            for key in (
                "actor",
                "tool_name",
                "decision",
                "rules_matched",
                "params_digest",
                "outcome",
            ):
                merged[key] = str(data.get(key, ""))
            out.append(merged)
        return out
