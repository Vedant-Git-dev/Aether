"""The operator migration tooling, rehearsed with the tests-only vault.

Synthetic legacy rows (active key, a rotated previous key, keyed tokens)
are re-sealed under a maintenance scope; the run is idempotent, resumable,
refuses on a missing historical key or an event tombstone, and leaves a
target with no legacy pages.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fake_vault import FakeVault

from aether.authz.audit import AuditLog, checkpoint_bytes, matches_checkpoint
from aether.config import ContactsConfig, MediaConfig
from aether.connectors.processed import ProcessedUpdates
from aether.media.store import MediaStore
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.db import run_migrations
from aether.memory.deliveries import Deliveries
from aether.memory.events import EventStore, content_hash
from aether.memory.storage import ScopedStore
from aether.providers.crypto import TokenKeyring
from aether.providers.store import ProviderStore
from aether.providers.types import ProviderConnection
from aether.vault.operator_migrate import (
    LegacyKeys,
    MaintenanceRefused,
    migrate,
)
from aether.vault.schema import AE2_MAGIC, CATEGORIES
from aether.vault.scope import VaultLocked
from aether.vault.store import VaultStore
from aether.workbench.transfers import TransferStore
from aether.workspace.store import Workspace

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
JOB = bytes(range(32, 48))
ALL_REFS = [(cat, 1) for cat in CATEGORIES]


@pytest.fixture(scope="session")
async def shadow_dsn():
    dsn = os.environ["AETHER_TEST_DATABASE_URL"]
    if supplied := os.environ.get("AETHER_TEST_SHADOW_DATABASE_URL"):
        conn = await asyncpg.connect(supplied)
        try:
            name = await conn.fetchval("SELECT current_database()")
            if not name.startswith("aether_test_shadow_") or urlsplit(dsn).path == f"/{name}":
                raise ValueError("the migration tests need a separate aether_test_shadow_ database")
        finally:
            await conn.close()
        yield supplied
        return
    name = f"aether_test_shadow_{secrets.token_hex(8)}"
    admin = await asyncpg.connect(dsn, command_timeout=90)
    try:
        await admin.execute(f"CREATE DATABASE {name}")  # noqa: S608 - generated identifier
        try:
            parsed = urlsplit(dsn)
            shadow_url = (
                urlunsplit(parsed._replace(path=f"/{name}"))
                if parsed.netloc
                else f"{parsed.scheme}:///{name}?{parsed.query}"
            )
            yield shadow_url
        finally:
            await admin.execute(f"DROP DATABASE {name} WITH (FORCE)", timeout=300)  # noqa: S608
    finally:
        await admin.close()


@pytest.fixture
async def shadow(shadow_dsn):
    async with asyncpg.create_pool(shadow_dsn, min_size=1, max_size=2) as pool:
        await run_migrations(pool)
        tables = await pool.fetch(
            "SELECT quote_ident(tablename) AS name FROM pg_tables"
            " WHERE schemaname = current_schema() AND tablename <> 'schema_migrations'"
        )
        names = ", ".join(row["name"] for row in tables)
        await pool.execute(f"TRUNCATE {names} RESTART IDENTITY")  # noqa: S608
        yield pool


def _legacy_cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def _keyring() -> TokenKeyring:
    return TokenKeyring(active_id="k1", active_key_b64=generate_key_b64())


async def _seed_legacy(db, active: Cipher, previous: Cipher, keyring: TokenKeyring) -> None:
    events = EventStore(db, active, AuditLog(db), ContactsConfig())
    await events.ingest(source="gmail", kind="message", payload={"text": "active key event"})
    rotated = EventStore(db, previous, AuditLog(db), ContactsConfig())
    event = await rotated.ingest(
        source="gmail",
        kind="message",
        payload={"text": "previous key event"},
        dedup_key="provider-message-42",
    )
    await rotated.merge_meta(event.event_id, {"scored": True})

    ws = Workspace(db, cipher=active)
    await ws.remember("a preference from before", "preference")

    media = MediaStore(db, active, MediaConfig(storage_quota_mb=16))
    await media.put(b"legacy attachment bytes", filename="a.bin", origin="telegram")

    deliveries = Deliveries(db, active)
    await deliveries.record_queued("telegram", "a queued reply")

    providers = ProviderStore(db, keyring, active)
    await providers.upsert(
        ProviderConnection(
            provider="google",
            auth_mode="oauth",
            access_token="access-legacy",
            refresh_token="refresh-legacy",
            account_label="me@example.test",
            scopes="mail",
        )
    )
    await providers.create_state(
        state_hash="b" * 64,
        provider="google",
        app="mail",
        scopes="mail",
        code_verifier="legacy-verifier",
        redirect_uri="https://example.test/cb",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def test_migration_rehearsal_covers_all_specs(db, shadow) -> None:
    active, previous = _legacy_cipher(), _legacy_cipher()
    keyring = _keyring()
    await _seed_legacy(db, active, previous, keyring)

    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "maintenance", ALL_REFS) as scope:
        report = await migrate(
            db,
            shadow,
            scope=scope,
            keys=LegacyKeys(active=active, previous=previous, keyring=keyring),
            job_id=JOB,
            tables=[
                "events",
                "workspace_files",
                "media_blobs",
                "deliveries",
                "provider_connections",
                "provider_oauth_states",
            ],
        )
        assert all(table.rows == table.migrated for table in report.tables)
        assert sum(table.migrated for table in report.tables) > 0

        # every sealed column is now an AE2 envelope, incl. the previous-key row
        for table in ("events", "workspace_files", "media_blobs", "deliveries"):
            column = {
                "events": "payload_enc",
                "workspace_files": "name_enc",
                "media_blobs": "bytes_enc",
                "deliveries": "body_enc",
            }[table]
            values = await shadow.fetch(f"SELECT {column} AS blob FROM {table}")  # noqa: S608
            assert values and all(bytes(v["blob"]).startswith(AE2_MAGIC) for v in values)

        # keyed tokens were re-derived under the maintenance scope
        event = await shadow.fetchrow(
            "SELECT source_token, dedup_mac FROM events ORDER BY id LIMIT 1"
        )
        assert event["source_token"] == scope.token("event.source", "gmail")
        media = await shadow.fetchrow("SELECT origin_token FROM media_blobs LIMIT 1")
        assert media["origin_token"] == scope.token("media.origin", "telegram")
        workspace = await shadow.fetchrow("SELECT name_token FROM workspace_files LIMIT 1")
        assert workspace["name_token"] == scope.token("workspace.name", "USER.md")

        source_blob = await db.fetchval("SELECT payload_enc FROM events ORDER BY id LIMIT 1")
        assert source_blob.startswith(b"AE1\0")
        assert "a preference from before" in await Workspace.scoped(shadow, scope).read("user")
        assert (await Deliveries.scoped(shadow, scope).undelivered())[0].body == "a queued reply"
        replay = await EventStore.scoped(shadow, scope, AuditLog(shadow), ContactsConfig()).ingest(
            source="gmail",
            kind="message",
            payload={"text": "updated body"},
            dedup_key="provider-message-42",
        )
        assert replay.reason == "duplicate"
        provider = ProviderStore.scoped(shadow, scope)
        assert (await provider.get("google")).access_token == "access-legacy"
        assert (await provider.consume_state("b" * 64)).code_verifier == "legacy-verifier"

        # a second run is idempotent and resumes from the journal
        again = await migrate(
            db,
            shadow,
            scope=scope,
            keys=LegacyKeys(active=active, previous=previous, keyring=keyring),
            job_id=JOB,
            tables=["events"],
        )
        events_report = again.for_table("events")
        assert events_report.migrated == 0
        assert events_report.resumed == events_report.rows


async def test_migration_refuses_a_missing_historical_key(db, shadow) -> None:
    writer = _legacy_cipher()
    await Deliveries(db, writer).record_queued("telegram", "sealed with a key we lost")
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "maintenance", ALL_REFS) as scope:
        with pytest.raises(MaintenanceRefused):
            await migrate(
                db,
                shadow,
                scope=scope,
                keys=LegacyKeys(active=_legacy_cipher()),  # neither active nor previous
                job_id=JOB,
                tables=["deliveries"],
            )


async def test_migration_refuses_on_event_tombstones(db, shadow) -> None:
    active = _legacy_cipher()
    await EventStore(db, active, AuditLog(db), ContactsConfig()).ingest(
        source="gmail", kind="message", payload={"text": "forgotten"}
    )
    await db.execute(
        "INSERT INTO forgotten_events (dedup_mac, source_token) VALUES ($1, $2)",
        secrets.token_bytes(32),
        secrets.token_bytes(32),
    )
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "maintenance", ALL_REFS) as scope:
        with pytest.raises(MaintenanceRefused):
            await migrate(
                db,
                shadow,
                scope=scope,
                keys=LegacyKeys(active=active),
                job_id=JOB,
                tables=["events"],
            )


async def test_migration_requires_the_maintenance_categories(db, shadow) -> None:
    active = _legacy_cipher()
    await Deliveries(db, active).record_queued("telegram", "x")
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        with pytest.raises(MaintenanceRefused, match="dedicated maintenance"):
            await migrate(
                db,
                shadow,
                scope=scope,
                keys=LegacyKeys(active=active),
                job_id=JOB,
                tables=["deliveries"],
            )
    async with vault.acquire(TASK, "maintenance", [("memory", 1)]) as scope:
        with pytest.raises(VaultLocked):
            await migrate(
                db,
                shadow,
                scope=scope,
                keys=LegacyKeys(active=active),
                job_id=JOB,
                tables=["deliveries"],
            )


async def test_migration_rolls_back_row_when_journal_fails_and_resumes(db, shadow) -> None:
    old = _legacy_cipher()
    await Deliveries(db, old).record_queued("telegram", "first")
    await Deliveries(db, old).record_queued("telegram", "second")

    class InterruptedJournal(VaultStore):
        writes = 0

        async def journal(self, **kwargs):
            self.writes += 1
            await super().journal(**kwargs)
            if self.writes == 2:
                raise RuntimeError("interrupted")

    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {"scope": scope, "keys": LegacyKeys(old), "job_id": JOB, "tables": ["deliveries"]}
        with pytest.raises(RuntimeError, match="interrupted"):
            await migrate(db, shadow, store=InterruptedJournal(shadow), **args)
        assert await shadow.fetchval("SELECT count(*) FROM deliveries") == 1
        assert await shadow.fetchval("SELECT count(*) FROM vault_migration_journal") == 1
        report = (await migrate(db, shadow, **args)).tables[0]
        assert (report.rows, report.resumed, report.migrated) == (2, 1, 1)


async def test_migration_refuses_unreconstructible_provider_dedup(db, shadow) -> None:
    old = _legacy_cipher()
    event = await EventStore(db, old, AuditLog(db), ContactsConfig()).ingest(
        source="gmail",
        kind="message",
        payload={"text": "synthetic"},
        dedup_key="provider-message-id",
    )
    aad = f"events:meta_enc:{event.event_id}"
    blob = await db.fetchval("SELECT meta_enc FROM events WHERE id = $1", event.event_id)
    old_meta = old.decrypt_json(blob, aad)
    del old_meta["dedup_key"]
    await db.execute("UPDATE events SET meta_enc = $1", old.encrypt_json(old_meta, aad))
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        with pytest.raises(MaintenanceRefused, match="dedup_mac input"):
            await migrate(
                db, shadow, scope=scope, keys=LegacyKeys(old), job_id=JOB, tables=["events"]
            )
    assert await db.fetchval("SELECT count(*) FROM events") == 1
    assert await shadow.fetchval("SELECT count(*) FROM events") == 0


async def test_legacy_connector_receipts_and_historical_oauth_tokens(db, shadow) -> None:
    old = _legacy_cipher()
    await ProcessedUpdates(db).record("telegram", 42)
    await db.execute(
        "INSERT INTO oauth_tokens (provider, access_enc, refresh_enc, expires_at)"
        " VALUES ('google', $1, $2, now())",
        old.encrypt_text("historical access", "oauth_tokens:access_enc:google"),
        old.encrypt_text("historical refresh", "oauth_tokens:refresh_enc:google"),
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        await migrate(
            db,
            shadow,
            scope=scope,
            keys=LegacyKeys(old),
            job_id=JOB,
            tables=["processed_updates", "oauth_tokens"],
        )
        receipt = await shadow.fetchrow("SELECT update_id, dedup_token FROM processed_updates")
        assert receipt["update_id"] is None and receipt["dedup_token"] is not None
        assert not await ProcessedUpdates.scoped(shadow, scope).record("telegram", 42)
        assert await ProcessedUpdates.scoped(shadow, scope).record("telegram", 43)
        assert await db.fetchval("SELECT update_id FROM processed_updates") == 42
        blob = await shadow.fetchval("SELECT access_enc FROM oauth_tokens")
        assert (
            ScopedStore(scope, cat="credentials", ver=1).unseal_bytes(
                blob,
                table="oauth_tokens",
                column="access_enc",
                row_id="google",
            )
            == b"historical access"
        )


async def test_workbench_transfer_survives_migration_and_remains_single_use(db, shadow) -> None:
    old = _legacy_cipher()
    ticket = await TransferStore(db, old).mint(
        kind="bundle", payload={"text": "synthetic"}, ttl_seconds=300
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["workbench_transfers"],
        }
        await migrate(db, shadow, **args)
        report = await migrate(db, shadow, **args)
        assert report.tables[0].resumed == 1
        transfers = TransferStore.scoped(shadow, scope)
        assert await transfers.take_bundle(ticket.token) == {"text": "synthetic"}
        assert await transfers.take_bundle(ticket.token) is None


async def test_pre_cutover_unprefixed_event_copies_to_fresh_target(db, shadow) -> None:
    await db.execute("CREATE SCHEMA migration_legacy_events")
    pool = await asyncpg.create_pool(
        os.environ["AETHER_TEST_DATABASE_URL"],
        min_size=1,
        max_size=2,
        server_settings={"search_path": "migration_legacy_events"},
    )
    try:
        await pool.execute(
            "CREATE TABLE events (id BIGSERIAL PRIMARY KEY, source TEXT, kind TEXT,"
            " occurred_at TIMESTAMPTZ DEFAULT now(), content_hash TEXT, salience_score REAL,"
            " memorable BOOLEAN, meta JSONB, payload_enc BYTEA)"
        )
        raw_key = secrets.token_bytes(32)
        nonce = secrets.token_bytes(12)
        payload = {"text": "synthetic pre-cutover event"}
        ciphertext = nonce + AESGCM(raw_key).encrypt(
            nonce, b'{"text":"synthetic pre-cutover event"}', b"events:payload_enc:1"
        )
        await pool.execute(
            "INSERT INTO events (source, kind, content_hash, salience_score,"
            " memorable, meta, payload_enc) VALUES ('test', 'message', $1, 0, false, '{}', $2)",
            content_hash("message", payload),
            ciphertext,
        )
        async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
            await migrate(
                pool,
                shadow,
                scope=scope,
                keys=LegacyKeys(Cipher(raw_key)),
                job_id=JOB,
                tables=["events"],
            )
            events = EventStore.scoped(shadow, scope, AuditLog(shadow), ContactsConfig())
            assert (await events.get(1)).payload == payload
            assert await pool.fetchval("SELECT payload_enc FROM events") == ciphertext
    finally:
        await pool.close()
        await db.execute("DROP TABLE migration_legacy_events.events")
        await db.execute("DROP SCHEMA migration_legacy_events")


async def test_audit_archive_keeps_history_and_checks_signed_heads(db, shadow) -> None:
    audit = AuditLog(db)
    await audit.append(actor="owner", tool_name="example", decision="allow", params={"amount": 5})
    old = _legacy_cipher()
    from aether.memory.rows import CipherRows

    await AuditLog(db, CipherRows(old)).append(actor="system", tool_name="new", decision="info")
    checkpoint = await audit.verify_chain()
    signer = Ed25519PrivateKey.generate()
    original_signature = signer.sign(
        checkpoint_bytes(checkpoint, vault_id=VAULT_ID, job_id=JOB, archived=False)
    )

    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["audit_log"],
            "audit_signer": signer.public_key(),
        }
        with pytest.raises(MaintenanceRefused, match="signed checkpoint"):
            await migrate(db, shadow, **args, audit_signature=b"wrong")
        assert await shadow.fetchval("SELECT count(*) FROM audit_log") == 0
        result = await migrate(db, shadow, **args, audit_signature=original_signature)
        assert result.tables[0].migrated == 2
        restored = AuditLog.scoped(shadow, scope)
        display = await restored.recent()
        assert [row["tool_name"] for row in display] == ["", "new", "example"]
        assert (
            await shadow.fetchval("SELECT count(*) FROM audit_log WHERE params_digest <> ''") == 0
        )
        head = await restored.verify_chain()
        assert head.ok and head.entries == 3
        target_signature = signer.sign(
            checkpoint_bytes(head, vault_id=VAULT_ID, job_id=JOB, archived=True)
        )
        await shadow.execute("DELETE FROM audit_log WHERE seq = 3")
        truncated = await restored.verify_chain()
        assert not matches_checkpoint(
            truncated,
            vault_id=VAULT_ID,
            job_id=JOB,
            archived=True,
            signature=target_signature,
            signer=signer.public_key(),
        )
