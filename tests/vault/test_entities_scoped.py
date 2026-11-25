"""Scoped identity storage: AE2 profiles, handles and notes under a task
scope, tested against scratch Postgres with the tests-only fake vault.

The platform and normalized-handle joins are preserved exactly; the legacy
serving path stays intact.
"""

from __future__ import annotations

import pytest
from fake_vault import FakeVault

from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.entities import Entities, normalize_handle
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import VaultClosed, VaultLocked

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))


class _NoMatchJudge:
    async def confirm(self, a: object, b: object) -> float:
        return 0.0


class _NoSqlPool:
    """Any query is a test failure: a denied read must issue zero SQL."""

    async def fetchrow(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetch(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetchval(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def execute(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    def acquire(self) -> None:
        raise AssertionError("a denied read issued SQL")


async def test_scoped_identity_round_trip_and_joins(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        entities = Entities.scoped(db, scope, AuditLog(db), _NoMatchJudge())
        entity = await entities.resolve("telegram", "@Alice", "Alice")
        assert entity.display_name == "Alice"
        row = await db.fetchrow(
            "SELECT profile_enc, key_version FROM identities WHERE id = $1", entity.id
        )
        assert row["profile_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        handle = await db.fetchrow(
            "SELECT handle_enc, platform_token, normalized_token, key_version"
            " FROM identity_handles WHERE identity_id = $1",
            entity.id,
        )
        normalized = normalize_handle("telegram", "@Alice")
        assert handle["handle_enc"].startswith(AE2_MAGIC)
        assert handle["key_version"] == "v1"
        assert handle["platform_token"] == scope.token("identity.platform", "telegram")
        assert handle["normalized_token"] == scope.token("identity.handle", normalized)
        got = await entities.get(entity.id)
        assert got is not None
        assert got.display_name == "Alice"
        assert got.handles == [("telegram", "@Alice")]
        exact = await entities.resolve_exact("telegram", "@Alice")
        assert exact is not None and exact.id == entity.id
        assert [person.id for person in await entities.list_people()] == [entity.id]
        await entities.note(entity.id, {"kind": "note", "text": "met at the conference"})
        notes = await entities.notes_for(entity.id)
        assert notes[0].payload["text"] == "met at the conference"


async def test_scoped_identity_denied_reads_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", [("workspace", 1)]) as scope:
        entities = Entities.scoped(pool, scope, AuditLog(pool), _NoMatchJudge())
        with pytest.raises(VaultLocked):
            await entities.get(1)
        with pytest.raises(VaultLocked):
            await entities.list_people()
        with pytest.raises(VaultLocked):
            await entities.resolve_exact("telegram", "@Alice")
        with pytest.raises(VaultLocked):
            await entities.notes_for(1)
        with pytest.raises(VaultLocked):
            await entities.resolve("telegram", "@Alice", "Alice")
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        entities = Entities.scoped(pool, scope, AuditLog(pool), _NoMatchJudge())
    with pytest.raises(VaultClosed):
        await entities.get(1)


async def test_scoped_identity_corrupt_body_raises(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        entities = Entities.scoped(db, scope, AuditLog(db), _NoMatchJudge())
        entity = await entities.resolve("telegram", "@Alice", "Alice")
        profile = await db.fetchval("SELECT profile_enc FROM identities WHERE id = $1", entity.id)
        tampered = bytearray(profile)
        tampered[-1] ^= 0xFF
        await db.execute(
            "UPDATE identities SET profile_enc = $1 WHERE id = $2", bytes(tampered), entity.id
        )
        with pytest.raises(VaultProtocolError):
            await entities.get(entity.id)
        # a corrupt note fails closed too, rather than vanishing from the list
        await entities.note(entity.id, {"kind": "note", "text": "x"})
        note = await db.fetchrow(
            "SELECT id, payload_enc FROM entity_notes WHERE identity_id = $1", entity.id
        )
        broken = bytearray(note["payload_enc"])
        broken[-1] ^= 0xFF
        await db.execute(
            "UPDATE entity_notes SET payload_enc = $1 WHERE id = $2", bytes(broken), note["id"]
        )
        with pytest.raises(VaultProtocolError):
            await entities.notes_for(entity.id)


async def test_legacy_identity_path_is_unchanged(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    entities = Entities(db, cipher, AuditLog(db), _NoMatchJudge())
    entity = await entities.resolve("telegram", "@Bob", "Bob")
    assert entity.display_name == "Bob"
    await entities.note(entity.id, {"kind": "note", "text": "legacy"})
    notes = await entities.notes_for(entity.id)
    assert notes[0].payload["text"] == "legacy"
