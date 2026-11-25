"""Scoped workspace, media and held-note storage against scratch Postgres
with the tests-only fake vault.

The workspace name join is preserved (`workspace.name`), media keeps its
content dedup and origin tokens, and a scoped read of a corrupt blob or a
legacy-key row fails closed instead of returning empty.
"""

from __future__ import annotations

import pytest
from fake_vault import FakeVault

from aether.config import MediaConfig
from aether.media.store import MediaStore
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.held_notes import DbHeldNotes
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import LegacyCiphertextError, VaultClosed, VaultLocked
from aether.workspace.store import Workspace

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
WORKSPACE = [("workspace", 1)]
MEDIA = [("media", 1)]
MEMORY = [("memory", 1)]


class _NoSqlPool:
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


async def test_scoped_workspace_round_trip_and_name_join(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", WORKSPACE) as scope:
        ws = Workspace.scoped(db, scope)
        await ws.remember("prefers concise notifications", "preference")
        row = await db.fetchrow(
            "SELECT name_enc, name_token, content_enc, key_version FROM workspace_files"
            " WHERE name_token = $1",
            scope.token("workspace.name", "USER.md"),
        )
        assert row is not None
        assert row["name_enc"].startswith(AE2_MAGIC)
        assert row["content_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        assert "prefers concise notifications" in await ws.read("user")


async def test_scoped_media_round_trip_and_dedup(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    config = MediaConfig(storage_quota_mb=16)
    async with vault.acquire(TASK, "panel_read", MEDIA) as scope:
        media = MediaStore.scoped(db, scope, config)
        ref = await media.put(b"a small png", filename="x.png", origin="telegram")
        row = await db.fetchrow(
            "SELECT bytes_enc, meta_enc, dedup_mac, origin_token FROM media_blobs WHERE id = $1",
            ref.media_id,
        )
        assert row["bytes_enc"].startswith(AE2_MAGIC)
        assert row["meta_enc"].startswith(AE2_MAGIC)
        assert row["dedup_mac"] == scope.dedup("media.content", b"a small png")
        assert row["origin_token"] == scope.token("media.origin", "telegram")
        record = await media.get(ref.media_id)
        assert record is not None and record.data == b"a small png"
        # the same bytes are one row
        again = await media.put(b"a small png", origin="telegram")
        assert again.media_id == ref.media_id


async def test_scoped_held_notes_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", MEMORY) as scope:
        notes = DbHeldNotes.scoped(db, scope)
        await notes.hold("say it later")
        pending = await notes.pending()
        assert [n.text for n in pending] == ["say it later"]
        raw = await db.fetchval("SELECT body_enc FROM held_notes WHERE id = $1", pending[0].id)
        assert raw.startswith(AE2_MAGIC)


async def test_scoped_storage_denied_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", MEMORY) as scope:
        with pytest.raises(VaultLocked):
            await Workspace.scoped(pool, scope).is_empty()
        with pytest.raises(VaultLocked):
            await MediaStore.scoped(pool, scope, MediaConfig()).get(1)
    async with vault.acquire(TASK, "turn", WORKSPACE) as scope:
        scoped_ws = Workspace.scoped(pool, scope)
    with pytest.raises(VaultClosed):
        await scoped_ws.is_empty()


async def test_scoped_media_corrupt_bytes_fail_closed(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "panel_read", MEDIA) as scope:
        media = MediaStore.scoped(db, scope, MediaConfig())
        ref = await media.put(b"payload bytes")
        blob = await db.fetchval("SELECT bytes_enc FROM media_blobs WHERE id = $1", ref.media_id)
        broken = bytearray(blob)
        broken[-1] ^= 0xFF
        await db.execute(
            "UPDATE media_blobs SET bytes_enc = $1 WHERE id = $2", bytes(broken), ref.media_id
        )
        with pytest.raises(VaultProtocolError):
            await media.get(ref.media_id)


async def test_scoped_workspace_rejects_a_legacy_key_row(db) -> None:
    legacy = Cipher.from_b64(generate_key_b64())
    legacy_ws = Workspace(db, cipher=legacy)
    await legacy_ws.remember("written before the vault", "preference")
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", WORKSPACE) as scope:
        scoped = Workspace.scoped(db, scope)
        # the legacy row is invisible under the scoped name token, and when
        # it is reached by the scoped token its AE1 body fails closed
        await db.execute(
            "UPDATE workspace_files SET name_token = $1 WHERE name_enc = $2",
            scope.token("workspace.name", "USER.md"),
            (await db.fetchval("SELECT name_enc FROM workspace_files ORDER BY id LIMIT 1")),
        )
        with pytest.raises(LegacyCiphertextError):
            await scoped.read("user")
