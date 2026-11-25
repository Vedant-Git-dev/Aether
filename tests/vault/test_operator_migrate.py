"""The operator migration tooling, rehearsed with the tests-only vault.

Synthetic legacy rows (active key, a rotated previous key, keyed tokens)
are re-sealed under a maintenance scope; the run is idempotent, resumable,
refuses on a missing historical key or an event tombstone, and leaves a
target with no legacy pages.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import warnings
from datetime import UTC, datetime, timedelta
from getpass import GetPassWarning
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fake_vault import FakeVault
from software_vault import VaultDenied, open_grant
from test_software_vault import NOW, boot_pair, make_request, new_vault, pin_operator

from aether.alerts.notifications import AlertNotifications
from aether.alerts.spec import AlertEvent, AlertSpec
from aether.alerts.store import AlertStore
from aether.authz.audit import AuditLog, checkpoint_bytes, matches_checkpoint
from aether.config import ContactsConfig, MediaConfig
from aether.connectors.processed import ProcessedUpdates
from aether.media.store import MediaStore
from aether.memory.conclusions import Conclusions
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.db import run_migrations
from aether.memory.deliveries import Deliveries
from aether.memory.events import EventStore, content_hash
from aether.memory.focus import Focus, FocusState
from aether.memory.inbound import DbInboundStates
from aether.memory.storage import ScopedStore
from aether.origin import owner_message
from aether.proactive.store import ProactiveStore
from aether.providers.crypto import TokenKeyring
from aether.providers.store import ProviderStore
from aether.providers.types import ProviderConnection
from aether.vault.operator_migrate import (
    LegacyKeyIds,
    LegacyKeys,
    MaintenanceRefused,
    migrate,
    migrate_workspace_disk,
    read_legacy_keys,
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


async def test_legacy_key_input_refuses_echo_and_unapproved_tasks(monkeypatch) -> None:
    def echo_fallback(_prompt):
        warnings.warn("cannot disable echo", GetPassWarning, stacklevel=2)
        pytest.fail("fallback must never read a legacy key")

    monkeypatch.setattr("aether.vault.operator_migrate.getpass", echo_fallback)
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        with pytest.raises(MaintenanceRefused, match="open maintenance scope"):
            read_legacy_keys(scope, active_id="k1")
    async with vault.acquire(TASK, "maintenance", [("memory", 1)]) as scope:
        with pytest.raises(MaintenanceRefused, match="protected input"):
            read_legacy_keys(scope, active_id="k1")
        key = generate_key_b64()
        monkeypatch.setattr("aether.vault.operator_migrate.getpass", lambda _: key)
        keys = read_legacy_keys(scope, active_id="old")
        blob = Cipher.from_b64(key, key_id="old").encrypt_bytes(b"sample", "sample:aad")
        assert keys.active.decrypt_bytes(blob, "sample:aad") == b"sample"


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


async def test_owner_consented_test_vault_grant_migrates_delivery(db, shadow, monkeypatch) -> None:
    legacy_key = generate_key_b64()
    old = Cipher.from_b64(legacy_key)
    await Deliveries(db, old).record_queued("telegram", "synthetic pending reply")
    phone = new_vault()
    private, public = boot_pair()
    request, body = make_request(
        phone,
        epk=public,
        operation="maintenance",
        use_case="maintenance",
        refs=[{"cat": "operations", "ver": 1}],
        task=TASK,
    )
    request, origin = pin_operator(phone, body)
    with pytest.raises(VaultDenied) as denied:
        phone.issue(request, now=NOW, origin=origin)
    assert denied.value.reason == "user_required"
    phone.consent(request, origin=origin)
    grant = phone.issue(request, now=NOW, origin=origin)
    _, keys = open_grant(
        grant.envelope,
        body,
        phone_public=phone.phone_public,
        boot_private=private.private_bytes_raw(),
        now=NOW,
    )
    vault = FakeVault(vault_id=VAULT_ID, keys=keys)
    monkeypatch.setattr("aether.vault.operator_migrate.getpass", lambda _: legacy_key)
    async with vault.acquire(TASK, "maintenance", list(keys)) as scope:
        await migrate(
            db,
            shadow,
            scope=scope,
            key_ids=LegacyKeyIds(old.key_id),
            job_id=JOB,
            tables=["deliveries"],
        )
        pending = await Deliveries.scoped(shadow, scope).undelivered()
        assert pending[0].body == "synthetic pending reply"
    with pytest.raises(VaultDenied):
        phone.issue(request, now=NOW, origin=origin)
    phone.end_maintenance()


async def test_full_empty_migration_accepts_initialized_shadow_vault(db, shadow) -> None:
    store = VaultStore(shadow)
    await store.create(
        vault_id=VAULT_ID,
        device_id=TASK,
        digest=bytes(32),
        manifest=b"synthetic manifest",
        pins=b"synthetic pins",
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        result = await migrate(
            db, shadow, scope=scope, keys=LegacyKeys(_legacy_cipher()), job_id=JOB
        )
        assert all(report.rows == 0 for report in result.tables)
        assert await store.load(VAULT_ID) is not None


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


async def test_disk_workspace_copy_is_repeatable_and_preserves_source(db, shadow, tmp_path) -> None:
    old = _legacy_cipher()
    user = tmp_path / "USER.md"
    user.write_text("synthetic preference", encoding="utf-8")
    daily = tmp_path / "memory"
    daily.mkdir()
    note = daily / "2026-11-24.md"
    original = b"aether-workspace\x01" + old.encrypt_text(
        "synthetic daily note", aad="workspace:file:memory/2026-11-24.md"
    )
    note.write_bytes(original)
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {"scope": scope, "keys": LegacyKeys(old), "job_id": JOB}
        first = await migrate_workspace_disk(shadow, tmp_path, **args)
        assert (first.rows, first.migrated) == (2, 2)
        second = await migrate_workspace_disk(shadow, tmp_path, **args)
        assert (second.rows, second.resumed) == (2, 2)
        workspace = Workspace.scoped(shadow, scope)
        assert await workspace.read("user") == "synthetic preference"
        assert await workspace.read("daily", date_str="2026-11-24") == "synthetic daily note"
        user.write_text("changed source", encoding="utf-8")
        with pytest.raises(MaintenanceRefused, match="conflicts"):
            await migrate_workspace_disk(shadow, tmp_path, **args)
    assert note.read_bytes() == original


async def test_ciphertext_only_backup_restores_pending_delivery(db, shadow, shadow_dsn) -> None:
    old = _legacy_cipher()
    canary = "synthetic-restore-canary-4a73"
    await Deliveries(db, old).record_queued("telegram", canary)
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "maintenance", ALL_REFS) as scope:
        await migrate(
            db, shadow, scope=scope, keys=LegacyKeys(old), job_id=JOB, tables=["deliveries"]
        )
        dump = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-owner",
            "--no-acl",
            "--dbname",
            shadow_dsn,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        backup, error = await dump.communicate()
        assert dump.returncode == 0, error.decode(errors="replace")
        assert canary.encode() not in backup

        name = f"aether_test_restore_{secrets.token_hex(8)}"
        await db.execute(f"CREATE DATABASE {name}")  # noqa: S608 - generated identifier
        restored = None
        try:
            parsed = urlsplit(shadow_dsn)
            restore_url = (
                urlunsplit(parsed._replace(path=f"/{name}"))
                if parsed.netloc
                else f"{parsed.scheme}:///{name}?{parsed.query}"
            )
            restore = await asyncio.create_subprocess_exec(
                "psql",
                "-X",
                "--set",
                "ON_ERROR_STOP=1",
                "--dbname",
                restore_url,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await restore.communicate(backup)
            assert restore.returncode == 0, error.decode(errors="replace")
            restored = await asyncpg.create_pool(restore_url, min_size=1, max_size=2)
            rows = await Deliveries.scoped(restored, scope).undelivered()
            assert len(rows) == 1 and rows[0].body == canary
        finally:
            if restored is not None:
                await restored.close()
            await db.execute(f"DROP DATABASE {name} WITH (FORCE)", timeout=300)  # noqa: S608


async def test_provider_sync_cursors_with_shared_provider_survive_resume(db, shadow) -> None:
    old = _legacy_cipher()
    provider = ProviderStore(db, _keyring(), old)
    await provider.set_sync("google", "mail", "cursor", "synthetic-mail-cursor")
    await provider.set_sync("google", "calendar", "cursor", "synthetic-calendar-cursor")
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["provider_sync_state"],
        }
        assert (await migrate(db, shadow, **args)).tables[0].migrated == 2
        assert (await migrate(db, shadow, **args)).tables[0].resumed == 2
        scoped = ProviderStore.scoped(shadow, scope)
        assert await scoped.get_sync("google", "mail", "cursor") == "synthetic-mail-cursor"
        assert await scoped.get_sync("google", "calendar", "cursor") == "synthetic-calendar-cursor"


async def test_counters_survive_shadow_migration_and_resume(db, shadow) -> None:
    await db.execute(
        "INSERT INTO daily_counters (day, searches, tasks) VALUES ('2026-11-24', 8, 3)"
    )
    await db.execute("INSERT INTO workbench_minutes (month, seconds) VALUES ('2026-11', 120)")
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(_legacy_cipher()),
            "job_id": JOB,
            "tables": ["daily_counters", "workbench_minutes"],
        }
        first = await migrate(db, shadow, **args)
        assert all(table.migrated == 1 for table in first.tables)
        second = await migrate(db, shadow, **args)
        assert all(table.resumed == 1 for table in second.tables)
        assert await shadow.fetchval("SELECT searches FROM daily_counters") == 8
        assert await shadow.fetchval("SELECT seconds FROM workbench_minutes") == 120


async def test_alerts_keep_conditions_and_legacy_watch_aad(db, shadow) -> None:
    old = _legacy_cipher()
    source = AlertStore(db, old, AuditLog(db))
    alert = await source.create(
        spec=AlertSpec(title="Synthetic invoice", scope="mail", contains="invoice"),
        origin=owner_message(),
    )
    assert alert is not None
    await db.execute(
        "UPDATE alerts SET spec_enc = $1 WHERE id = $2",
        old.encrypt_json(alert.spec.to_payload(), aad=f"watches:spec_enc:{alert.id}"),
        alert.id,
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["alerts"],
        }
        assert (await migrate(db, shadow, **args)).tables[0].migrated == 1
        assert (await migrate(db, shadow, **args)).tables[0].resumed == 1
        target = AlertStore.scoped(shadow, scope, AuditLog(shadow))
        restored = await target.get(alert.id)
        assert restored and restored.spec.contains == "invoice"


async def test_notification_queue_and_dedup_survive_migration(db, shadow) -> None:
    old = _legacy_cipher()
    alert = await AlertStore(db, old, AuditLog(db)).create(
        spec=AlertSpec(title="Synthetic", scope="mail", contains="invoice"),
        origin=owner_message(),
    )
    assert alert is not None
    event = AlertEvent(alert_id=alert.id, event_id=42, source="mail", kind="message")
    assert await AlertNotifications(db, old).enqueue(event, style="immediate")
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["alerts", "alert_notifications"],
        }
        await migrate(db, shadow, **args)
        assert all(report.resumed == 1 for report in (await migrate(db, shadow, **args)).tables)
        notifications = AlertNotifications.scoped(shadow, scope)
        assert len(await notifications.recent()) == 1
        assert await notifications.enqueue(event, style="immediate") is None


async def test_notification_with_unrecoverable_custom_key_is_refused(db, shadow) -> None:
    old = _legacy_cipher()
    alert = await AlertStore(db, old, AuditLog(db)).create(
        spec=AlertSpec(title="Synthetic", scope="mail", contains="invoice"),
        origin=owner_message(),
    )
    assert alert is not None
    event = AlertEvent(alert_id=alert.id, event_id=42, source="mail", kind="message")
    notification_id = await AlertNotifications(db, old).enqueue(
        event, style="immediate", key="synthetic-custom-key"
    )
    assert notification_id is not None
    aad = f"alert_notifications:event_enc:{notification_id}"
    blob = await db.fetchval(
        "SELECT event_enc FROM alert_notifications WHERE id = $1", notification_id
    )
    legacy = old.decrypt_json(blob, aad)
    del legacy["dedup_key"]
    await db.execute(
        "UPDATE alert_notifications SET event_enc = $1 WHERE id = $2",
        old.encrypt_json(legacy, aad),
        notification_id,
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        await migrate(db, shadow, scope=scope, keys=LegacyKeys(old), job_id=JOB, tables=["alerts"])
        with pytest.raises(MaintenanceRefused, match="key_token input"):
            await migrate(
                db,
                shadow,
                scope=scope,
                keys=LegacyKeys(old),
                job_id=JOB,
                tables=["alert_notifications"],
            )
    assert await shadow.fetchval("SELECT count(*) FROM alert_notifications") == 0


async def test_notification_with_recorded_custom_key_remains_deduplicated(db, shadow) -> None:
    old = _legacy_cipher()
    alert = await AlertStore(db, old, AuditLog(db)).create(
        spec=AlertSpec(title="Synthetic", scope="mail", contains="invoice"),
        origin=owner_message(),
    )
    assert alert is not None
    event = AlertEvent(alert_id=alert.id, event_id=42, source="mail", kind="message")
    assert await AlertNotifications(db, old).enqueue(
        event, style="immediate", key="synthetic-custom-key"
    )
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        await migrate(
            db,
            shadow,
            scope=scope,
            keys=LegacyKeys(old),
            job_id=JOB,
            tables=["alerts", "alert_notifications"],
        )
        notifications = AlertNotifications.scoped(shadow, scope)
        assert len(await notifications.recent()) == 1
        assert (
            await notifications.enqueue(event, style="immediate", key="synthetic-custom-key")
            is None
        )


async def test_proactive_claim_remains_unique_after_migration(db, shadow) -> None:
    old = _legacy_cipher()
    source = ProactiveStore(db, old, AuditLog(db))
    task = await source.claim(
        skill="mail",
        key="synthetic-thread",
        label="Synthetic task",
        opportunity={"message": "synthetic"},
        day="2026-11-24",
    )
    assert task is not None
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["proactive_tasks"],
        }
        assert (await migrate(db, shadow, **args)).tables[0].migrated == 1
        assert (await migrate(db, shadow, **args)).tables[0].resumed == 1
        target = ProactiveStore.scoped(shadow, scope, AuditLog(shadow))
        assert (await target.get(task.id)).opportunity == {"message": "synthetic"}
        assert (
            await target.claim(
                skill="mail",
                key="synthetic-thread",
                label="Duplicate",
                opportunity={"message": "changed"},
                day="2026-11-24",
            )
            is None
        )


async def test_inbound_identity_survives_migration_and_replay(db, shadow) -> None:
    old = _legacy_cipher()
    source = DbInboundStates(db, old)
    inbound = await source.record("telegram", "synthetic-chat", "synthetic-message")
    assert inbound is not None
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["inbound_messages"],
        }
        assert (await migrate(db, shadow, **args)).tables[0].migrated == 1
        assert (await migrate(db, shadow, **args)).tables[0].resumed == 1
        target = DbInboundStates.scoped(shadow, scope)
        assert (await target.get(inbound.id)).status == "received"
        assert await target.record("telegram", "synthetic-chat", "synthetic-message") is None


async def test_focus_and_conclusion_lookups_survive_migration(db, shadow) -> None:
    old = _legacy_cipher()
    await Focus(db, old).save("synthetic-chat", FocusState(open_task={"text": "synthetic"}))
    await Conclusions(db, old).set("synthetic-question", {"answer": "synthetic"})
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        args = {
            "scope": scope,
            "keys": LegacyKeys(old),
            "job_id": JOB,
            "tables": ["focus", "conclusions"],
        }
        assert all(item.migrated == 1 for item in (await migrate(db, shadow, **args)).tables)
        assert all(item.resumed == 1 for item in (await migrate(db, shadow, **args)).tables)
        focus = await Focus.scoped(shadow, scope).get("synthetic-chat")
        conclusion = await Conclusions.scoped(shadow, scope).get("synthetic-question")
        assert focus and focus.open_task == {"text": "synthetic"}
        assert conclusion and conclusion.value == {"answer": "synthetic"}


async def test_legacy_focus_without_lookup_input_is_preserved_on_refusal(db, shadow) -> None:
    old = _legacy_cipher()
    await Focus(db, old).save("synthetic-chat", FocusState(open_task={"text": "synthetic"}))
    row = await db.fetchrow("SELECT chat_ref_token, state_enc FROM focus")
    aad = f"focus:state_enc:{bytes(row['chat_ref_token']).hex()}"
    body = old.decrypt_json(row["state_enc"], aad)
    del body["lookup_chat"]
    legacy = old.encrypt_json(body, aad)
    await db.execute("UPDATE focus SET state_enc = $1", legacy)
    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "maintenance", ALL_REFS) as scope:
        with pytest.raises(MaintenanceRefused, match="lookup input"):
            await migrate(
                db, shadow, scope=scope, keys=LegacyKeys(old), job_id=JOB, tables=["focus"]
            )
    assert await db.fetchval("SELECT state_enc FROM focus") == legacy
    assert await shadow.fetchval("SELECT count(*) FROM focus") == 0


async def test_confirmed_historical_tags_survive_migration_and_replay(db, shadow) -> None:
    old = _legacy_cipher()
    previous = Cipher.from_b64(generate_key_b64(), key_id="previous")
    chat = "synthetic-chat"
    key = "synthetic-answer"
    await Focus(db, old).save(chat, FocusState(open_task={"text": "synthetic"}))
    await Conclusions(db, old).set(key, "previous answer")
    await DbInboundStates(db, old).record("telegram", chat, "42")
    await db.execute("UPDATE inbound_messages SET identity_enc = NULL")
    for table, column, tag, field in (
        ("focus", "state_enc", old.blind_token("focus", chat), "lookup_chat"),
        ("conclusions", "value_enc", old.blind_token("conclusion", key), "lookup_key"),
    ):
        encrypted = await db.fetchval(f"SELECT {column} FROM {table}")  # noqa: S608
        aad = f"{table}:{column}:{tag.hex()}"
        body = old.decrypt_json(encrypted, aad)
        del body[field]
        await db.execute(
            f"UPDATE {table} SET {column} = $1",
            old.encrypt_json(body, aad),  # noqa: S608
        )
    await db.execute(
        "INSERT INTO forgotten_events (dedup_mac, source_token) VALUES ($1, $2)",
        previous.dedup_mac(b"dedup:synthetic-forgotten"),
        previous.blind_token("source", "gmail"),
    )

    class Lookup:
        def __init__(self, cipher, purpose):
            self.cipher = cipher
            self.purpose = purpose

        def tag(self, value):
            if self.purpose:
                return self.cipher.blind_token(self.purpose, value)
            return self.cipher.dedup_mac(value)

        def close(self):
            pass

    vault = FakeVault(vault_id=VAULT_ID)
    args = {
        "job_id": JOB,
        "keys": LegacyKeys(old, previous),
        "historical_source_complete": True,
        "tables": ["events", "focus", "conclusions", "inbound_messages"],
    }
    async with vault.acquire(TASK, "maintenance", ALL_REFS) as scope:
        with pytest.raises(MaintenanceRefused, match="missing a historical lookup key"):
            await migrate(db, shadow, scope=scope, **args)
        domains = {
            "event.dedup": None,
            "focus.chat": "focus",
            "conclusion.key": "conclusion",
            "inbound.key": "inbound-key",
            "inbound.chat": "inbound-chat",
            "inbound.message": "inbound-message",
        }
        scope._historical.update(
            {
                domain: (Lookup(old, purpose), Lookup(previous, purpose))
                for domain, purpose in domains.items()
            }
        )
        assert (await migrate(db, shadow, scope=scope, **args)).for_table(
            "forgotten_events"
        ).migrated == 1
        assert (await migrate(db, shadow, scope=scope, **args)).for_table(
            "forgotten_events"
        ).resumed == 1
        store = EventStore.scoped(shadow, scope, AuditLog(shadow), ContactsConfig())
        replay = await store.ingest(
            source="gmail", kind="message", payload={}, dedup_key="synthetic-forgotten"
        )
        assert replay.reason == "forgotten"
        assert await DbInboundStates.scoped(shadow, scope).record("telegram", chat, "42") is None
        focus = Focus.scoped(shadow, scope)
        assert (await focus.get(chat)).open_task == {"text": "synthetic"}
        await focus.save(chat, FocusState())
        assert (await focus.get(chat)).open_task is None
        conclusions = Conclusions.scoped(shadow, scope)
        assert (await conclusions.get(key)).value == "previous answer"
        await conclusions.forget(key)
        assert await conclusions.get(key) is None
        await conclusions.set(key, "current answer")
        assert (await conclusions.get(key)).value == "current answer"
        assert await shadow.fetchval("SELECT count(*) FROM focus") == 1
        assert await shadow.fetchval("SELECT count(*) FROM conclusions") == 1

    async with vault.acquire(TASK, "turn", [("memory", 1), ("operations", 1)]) as scope:
        store = EventStore.scoped(shadow, scope, AuditLog(shadow), ContactsConfig())
        scope._historical["event.dedup"] = (Lookup(old, None),)
        with pytest.raises(VaultLocked, match="historical"):
            await store.ingest(
                source="gmail", kind="message", payload={}, dedup_key="synthetic-forgotten"
            )
        with pytest.raises(VaultLocked, match="historical"):
            await Focus.scoped(shadow, scope).get(chat)
        with pytest.raises(VaultLocked, match="historical"):
            await DbInboundStates.scoped(shadow, scope).record("telegram", chat, "42")


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
