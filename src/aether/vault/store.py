"""SQL for the vault's own state, credentials, queue and migration journal.

The wire format and the scoped key boundary live elsewhere. This module only
stores public protocol metadata, opaque hashes and sealed envelopes. CAS is a
single conditional UPDATE, so two writers cannot both advance the manifest.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from ..memory.allowlist import PLAINTEXT_ALLOWLIST, SEALED_COLUMNS
from .policy import policy
from .schema import CATEGORIES, DIGEST_BYTES, ID_BYTES, MAX_SAFE_INTEGER
from .scope import ReservedWrites, VaultExhausted

_STATES = ("unpaired", "locked", "available", "revoked")
_CONTROL_FROM = {
    "locked": ["locked", "available"],
    "available": ["locked"],
    "revoked": ["locked", "available"],
}
_SESSION_KINDS = ("device", "panel")
_JOURNAL_STATES = ("pending", "done", "failed")


def _id(value: Any, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != ID_BYTES:
        raise ValueError(f"{name} must be {ID_BYTES} bytes")
    return bytes(value)


def _digest(value: Any, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != DIGEST_BYTES:
        raise ValueError(f"{name} must be {DIGEST_BYTES} bytes")
    return bytes(value)


def _bytes(value: Any, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or not value:
        raise ValueError(f"{name} must be non-empty bytes")
    return bytes(value)


class VaultStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        vault_id: bytes,
        device_id: bytes,
        digest: bytes,
        manifest: bytes,
        pins: bytes,
        epoch: int = 1,
        revision: int = 1,
        state: str = "locked",
        previous_digest: bytes | None = None,
    ) -> None:
        vault_id = _id(vault_id, "vault_id")
        device_id = _id(device_id, "device_id")
        digest = _digest(digest, "digest")
        if previous_digest is not None:
            previous_digest = _digest(previous_digest, "previous_digest")
        if state not in _STATES:
            raise ValueError(f"unknown vault state {state!r}")
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO vault_state"
                " (vault_id, device_id, epoch, revision, digest, manifest, pins, state)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                vault_id,
                device_id,
                epoch,
                revision,
                digest,
                _bytes(manifest, "manifest"),
                _bytes(pins, "pins"),
                state,
            )
            await conn.execute(
                "INSERT INTO vault_manifest"
                " (vault_id, revision, digest, previous_digest, manifest)"
                " VALUES ($1, $2, $3, $4, $5)",
                vault_id,
                revision,
                digest,
                previous_digest,
                _bytes(manifest, "manifest"),
            )

    async def load(self, vault_id: bytes) -> asyncpg.Record | None:
        return await self._pool.fetchrow(
            "SELECT vault_id, device_id, epoch, revision, digest, manifest, pins,"
            " state, generation, updated_at FROM vault_state WHERE vault_id = $1",
            _id(vault_id, "vault_id"),
        )

    async def publish_manifest(
        self,
        *,
        vault_id: bytes,
        expected_revision: int,
        expected_digest: bytes,
        expected_epoch: int,
        expected_generation: int,
        revision: int,
        digest: bytes,
        previous_digest: bytes,
        manifest: bytes,
    ) -> bool:
        """CAS the current manifest. Returns False for a stale writer."""
        vault_id = _id(vault_id, "vault_id")
        expected_digest = _digest(expected_digest, "expected_digest")
        digest = _digest(digest, "digest")
        previous_digest = _digest(previous_digest, "previous_digest")
        manifest = _bytes(manifest, "manifest")
        if revision != expected_revision + 1 or previous_digest != expected_digest:
            return False
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE vault_state SET revision = $1, digest = $2, manifest = $3,"
                " generation = generation + 1, updated_at = now()"
                " WHERE vault_id = $4 AND revision = $5 AND digest = $6"
                " AND epoch = $7 AND generation = $8 AND state IN ('locked', 'available')"
                " RETURNING revision",
                revision,
                digest,
                manifest,
                vault_id,
                expected_revision,
                expected_digest,
                expected_epoch,
                expected_generation,
            )
            if row is None:
                return False
            await conn.execute(
                "INSERT INTO vault_manifest"
                " (vault_id, revision, digest, previous_digest, manifest)"
                " VALUES ($1, $2, $3, $4, $5)",
                vault_id,
                revision,
                digest,
                previous_digest,
                manifest,
            )
            return True

    async def set_state(
        self, vault_id: bytes, state: str, *, expected_epoch: int, expected_generation: int
    ) -> bool:
        """Apply a verified control against the state it was admitted under."""
        if state not in _CONTROL_FROM:
            raise ValueError(f"unknown control target {state!r}")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE vault_state SET state = $1, epoch = epoch + 1,"
                " generation = generation + 1, updated_at = now()"
                " WHERE vault_id = $2 AND epoch = $3 AND generation = $4"
                " AND state = ANY($5::text[]) RETURNING generation",
                state,
                _id(vault_id, "vault_id"),
                expected_epoch,
                expected_generation,
                _CONTROL_FROM[state],
            )
        return row is not None

    async def reserve_aead(
        self, *, vault_id: bytes, cat: str, ver: int, count: int
    ) -> ReservedWrites:
        """Reserve writes before sealing. Unused writes are burned on failure or restart."""
        vault_id = _id(vault_id, "vault_id")
        if cat not in CATEGORIES or cat == "ingress":
            raise ValueError("expected a storage key category")
        if type(ver) is not int or not 1 <= ver <= MAX_SAFE_INTEGER:
            raise ValueError("key version must be a positive safe integer")
        if type(count) is not int or count < 1:
            raise ValueError("reservation count must be positive")
        limit = policy().aes_gcm_operational_threshold
        if count > limit:
            raise VaultExhausted("the requested encryption budget exceeds the key limit")
        balance = await self._pool.fetchval(
            "INSERT INTO vault_aead_budget (vault_id, category, version, reserved)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (vault_id, category, version) DO UPDATE"
            " SET reserved = vault_aead_budget.reserved + EXCLUDED.reserved"
            " WHERE vault_aead_budget.reserved <= $5::bigint - EXCLUDED.reserved"
            " RETURNING reserved",
            vault_id,
            cat,
            ver,
            count,
            limit,
        )
        if balance is None:
            raise VaultExhausted("the key's encryption budget is spent")
        return ReservedWrites(vault_id, cat, ver, count)

    # -- one-use credentials ----------------------------------------------

    async def create_ticket(self, *, ticket_hash: bytes, vault_id: bytes, expires_at: Any) -> None:
        await self._pool.execute(
            "INSERT INTO vault_ticket (ticket_hash, vault_id, expires_at) VALUES ($1, $2, $3)",
            _digest(ticket_hash, "ticket_hash"),
            _id(vault_id, "vault_id"),
            expires_at,
        )

    async def consume_ticket(self, ticket_hash: bytes) -> bytes | None:
        row = await self._pool.fetchrow(
            "UPDATE vault_ticket SET consumed_at = now()"
            " WHERE ticket_hash = $1 AND consumed_at IS NULL AND expires_at > now()"
            " RETURNING vault_id",
            _digest(ticket_hash, "ticket_hash"),
        )
        return None if row is None else bytes(row["vault_id"])

    async def create_challenge(
        self,
        *,
        challenge_hash: bytes,
        vault_id: bytes,
        device_id: bytes,
        purpose: str,
        expires_at: Any,
    ) -> None:
        await self._pool.execute(
            "INSERT INTO vault_challenge"
            " (challenge_hash, vault_id, device_id, purpose, expires_at)"
            " VALUES ($1, $2, $3, $4, $5)",
            _digest(challenge_hash, "challenge_hash"),
            _id(vault_id, "vault_id"),
            _id(device_id, "device_id"),
            purpose,
            expires_at,
        )

    async def consume_challenge(
        self, challenge_hash: bytes, *, vault_id: bytes, device_id: bytes, purpose: str
    ) -> asyncpg.Record | None:
        return await self._pool.fetchrow(
            "UPDATE vault_challenge SET consumed_at = now()"
            " WHERE challenge_hash = $1 AND consumed_at IS NULL AND expires_at > now()"
            " AND vault_id = $2 AND device_id = $3 AND purpose = $4"
            " RETURNING vault_id, device_id, purpose",
            _digest(challenge_hash, "challenge_hash"),
            _id(vault_id, "vault_id"),
            _id(device_id, "device_id"),
            purpose,
        )

    async def create_session(
        self,
        *,
        session_hash: bytes,
        vault_id: bytes,
        device_id: bytes | None,
        kind: str,
        epoch: int,
        expires_at: Any,
    ) -> None:
        if kind not in _SESSION_KINDS:
            raise ValueError(f"unknown session kind {kind!r}")
        await self._pool.execute(
            "INSERT INTO vault_session"
            " (session_hash, vault_id, device_id, kind, epoch, expires_at)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            _digest(session_hash, "session_hash"),
            _id(vault_id, "vault_id"),
            None if device_id is None else _id(device_id, "device_id"),
            kind,
            epoch,
            expires_at,
        )

    async def revoke_session(self, session_hash: bytes) -> bool:
        row = await self._pool.fetchrow(
            "UPDATE vault_session SET revoked_at = now()"
            " WHERE session_hash = $1 AND revoked_at IS NULL RETURNING session_hash",
            _digest(session_hash, "session_hash"),
        )
        return row is not None

    # -- ingress queue -----------------------------------------------------

    async def reserve_queue(self, *, queue_id: bytes, vault_id: bytes, ingress_ver: int) -> None:
        await self._pool.execute(
            "INSERT INTO vault_ingress_queue (queue_id, vault_id, ingress_ver) VALUES ($1, $2, $3)",
            _id(queue_id, "queue_id"),
            _id(vault_id, "vault_id"),
            ingress_ver,
        )

    async def seal_queue(self, queue_id: bytes, ciphertext: bytes) -> bool:
        queue_id = _id(queue_id, "queue_id")
        ciphertext = _bytes(ciphertext, "ciphertext")
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_open_queue(conn, queue_id):
                return False
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM vault_ingress_part WHERE queue_id = $1)", queue_id
            ):
                return False
            await conn.execute(
                "UPDATE vault_ingress_queue SET ciphertext_enc = $1, complete = TRUE,"
                " updated_at = now() WHERE queue_id = $2",
                ciphertext,
                queue_id,
            )
            return True

    @staticmethod
    async def _lock_open_queue(conn: asyncpg.Connection, queue_id: bytes) -> bool:
        return bool(
            await conn.fetchval(
                "SELECT NOT complete FROM vault_ingress_queue WHERE queue_id = $1 FOR UPDATE",
                queue_id,
            )
        )

    async def append_part(
        self, *, queue_id: bytes, part: int, kind: str, ciphertext: bytes
    ) -> bool:
        if part < 0:
            raise ValueError("part must be non-negative")
        if kind not in ("chunk", "end"):
            raise ValueError(f"unknown part kind {kind!r}")
        queue_id = _id(queue_id, "queue_id")
        ciphertext = _bytes(ciphertext, "ciphertext")
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_open_queue(conn, queue_id):
                return False
            next_part = await conn.fetchval(
                "SELECT COALESCE(MAX(part)::bigint + 1, 0)"
                " FROM vault_ingress_part WHERE queue_id = $1",
                queue_id,
            )
            if part != next_part:
                return False
            await conn.execute(
                "INSERT INTO vault_ingress_part (queue_id, part, kind, ciphertext_enc)"
                " VALUES ($1, $2, $3, $4)",
                queue_id,
                part,
                kind,
                ciphertext,
            )
            if kind == "end":
                await conn.execute(
                    "UPDATE vault_ingress_queue SET complete = TRUE, updated_at = now()"
                    " WHERE queue_id = $1",
                    queue_id,
                )
            return True

    async def queue_group(
        self, queue_id: bytes
    ) -> tuple[asyncpg.Record | None, list[asyncpg.Record]]:
        queue_id = _id(queue_id, "queue_id")
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                "SELECT queue_id, vault_id, ingress_ver, ciphertext_enc AS ciphertext, complete, created_at"
                " FROM vault_ingress_queue WHERE queue_id = $1",
                queue_id,
            )
            parts = await conn.fetch(
                "SELECT part, kind, ciphertext_enc AS ciphertext FROM vault_ingress_part"
                " WHERE queue_id = $1 ORDER BY part",
                queue_id,
            )
        return row, list(parts)

    # -- migration journal -------------------------------------------------

    async def journal(
        self,
        *,
        job_id: bytes,
        vault_id: bytes,
        table_name: str,
        stable_id: bytes,
        state: str,
        detail_enc: bytes | None = None,
        connection: asyncpg.Connection | None = None,
    ) -> None:
        if state not in _JOURNAL_STATES:
            raise ValueError(f"unknown journal state {state!r}")
        if table_name not in SEALED_COLUMNS and table_name not in PLAINTEXT_ALLOWLIST:
            raise ValueError("unknown journal table")
        executor = self._pool if connection is None else connection
        await executor.execute(
            "INSERT INTO vault_migration_journal"
            " (job_id, vault_id, table_name, stable_id, state, detail_enc)"
            " VALUES ($1, $2, $3, $4, $5, $6)"
            " ON CONFLICT (vault_id, job_id, table_name, stable_id) DO UPDATE"
            " SET state = EXCLUDED.state, detail_enc = EXCLUDED.detail_enc,"
            " updated_at = now()",
            _id(job_id, "job_id"),
            _id(vault_id, "vault_id"),
            table_name,
            _digest(stable_id, "stable_id"),
            state,
            detail_enc,
        )

    async def journal_entry(
        self,
        *,
        vault_id: bytes,
        job_id: bytes,
        table_name: str,
        stable_id: bytes,
        connection: asyncpg.Connection | None = None,
    ) -> asyncpg.Record | None:
        executor = self._pool if connection is None else connection
        return await executor.fetchrow(
            "SELECT job_id, vault_id, table_name, stable_id, state, detail_enc, updated_at"
            " FROM vault_migration_journal"
            " WHERE vault_id = $1 AND job_id = $2 AND table_name = $3 AND stable_id = $4",
            _id(vault_id, "vault_id"),
            _id(job_id, "job_id"),
            table_name,
            _digest(stable_id, "stable_id"),
        )


def queue_is_complete(row: asyncpg.Record | None, parts: list[asyncpg.Record]) -> bool:
    """Check the stored shape. Ingress still authenticates the encrypted content."""
    if row is None or not row["complete"]:
        return False
    if not parts:
        return bool(row["ciphertext"])
    return not row["ciphertext"] and all(
        part["part"] == index
        and part["ciphertext"]
        and part["kind"] == ("end" if index == len(parts) - 1 else "chunk")
        for index, part in enumerate(parts)
    )
