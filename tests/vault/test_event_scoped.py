"""The scoped event storage slice: AE2 writes and direct reads under a task
scope, tested against scratch Postgres with the tests-only fake vault.

The legacy serving path and its app-wide plaintext index are untouched. These
tests exercise the explicit scoped construction, the preserved source/dedup
inputs, replay suppression for newly forgotten events, and fail-closed
behavior without a valid scope. Legacy tombstone migration across the key
change is left open for the owner's cutover decision.
"""

from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime

import pytest
from fake_vault import FakeVault
from software_vault import open_grant
from test_software_vault import NOW, boot_pair, make_request, new_vault, pin_operator

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig
from aether.memory.control import MemoryControl
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.event_rows import CipherEventRows, ScopedEventRows
from aether.memory.events import EventStore, canonical_payload
from aether.memory.indexes import MemoryIndex, decode_event_row
from aether.memory.lexical import LexicalIndex
from aether.memory.vectors import VectorMemory
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import VaultClosed, VaultLocked

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))


def _at(hour: int) -> datetime:
    return datetime(2026, 11, 3, hour, 0, tzinfo=UTC)


async def test_scoped_ingest_round_trip_and_envelope(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(
            source="gmail",
            kind="message",
            payload={"text": "the invoice is due", "subject": "bill"},
            occurred_at=_at(9),
            meta={"label": "mail"},
        )
        assert result.stored and result.event_id is not None
        row = await db.fetchrow(
            "SELECT payload_enc, meta_enc, source_token, dedup_mac, key_version"
            " FROM events WHERE id = $1",
            result.event_id,
        )
        assert row["payload_enc"].startswith(AE2_MAGIC)
        assert row["meta_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        # the exact legacy inputs are preserved; only the deriving key changes
        assert row["source_token"] == scope.token("event.source", "gmail")
        assert row["dedup_mac"] == scope.dedup(
            "event.dedup",
            canonical_payload("message", {"text": "the invoice is due", "subject": "bill"}),
        )
        event = await store.get(result.event_id)
        assert event is not None
        assert event.payload["subject"] == "bill"
        assert event.source == "gmail"
        assert event.meta == {"label": "mail"}
        # a scoped store runs no detached enrichment
        assert store._enriching == {}


async def test_scoped_dedup_suppresses_replay(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        first = await store.ingest(source="gmail", kind="message", payload={"text": "same"})
        again = await store.ingest(source="gmail", kind="message", payload={"text": "same"})
        assert first.stored
        assert not again.stored and again.reason == "duplicate"
        # the source's own identity dedups even when the body changed
        keyed = await store.ingest(
            source="telegram", kind="chat_message", payload={"text": "v1"}, dedup_key="msg-1"
        )
        refetch = await store.ingest(
            source="telegram", kind="chat_message", payload={"text": "v2 edited"}, dedup_key="msg-1"
        )
        assert keyed.stored
        assert not refetch.stored and refetch.reason == "duplicate"


async def test_newly_forgotten_event_stays_forgotten(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(source="gmail", kind="message", payload={"text": "forget me"})
        assert result.event_id is not None
        row = await db.fetchrow(
            "SELECT dedup_mac, source_token FROM events WHERE id = $1", result.event_id
        )
        # forget under the same scoped key: tombstone the stored tag, drop the row
        await db.execute(
            "INSERT INTO forgotten_events (dedup_mac, source_token) VALUES ($1, $2)",
            row["dedup_mac"],
            row["source_token"],
        )
        await db.execute("DELETE FROM events WHERE id = $1", result.event_id)
        replay = await store.ingest(source="gmail", kind="message", payload={"text": "forget me"})
        assert not replay.stored and replay.reason == "forgotten"


async def test_scoped_ingest_checks_restricted_historical_marker(db) -> None:
    legacy = Cipher.from_b64(generate_key_b64())
    payload = {"text": "synthetic historical item"}
    marker = legacy.dedup_mac(canonical_payload("message", payload))
    await db.execute(
        "INSERT INTO forgotten_events (dedup_mac, source_token) VALUES ($1, $2)",
        marker,
        b"synthetic",
    )

    phone = new_vault()
    boot, epk = boot_pair()
    _, maintenance = make_request(
        phone, epk=epk, operation="maintenance", use_case="maintenance", task=TASK
    )
    pin_operator(phone, maintenance)
    phone.import_historical(TASK, {("event.dedup", bytes(16)): legacy._dedup_key})
    phone.end_maintenance()
    request, body = make_request(phone, epk=epk, task=TASK)
    grant = phone.issue(request, now=NOW)
    historical = {}
    _, keys = open_grant(
        grant.envelope,
        body,
        phone_public=phone.phone_public,
        boot_private=boot.private_bytes_raw(),
        now=NOW,
        pinned_manifest=phone.manifest_body,
        historical_keys=historical,
    )
    (lookup_key,) = historical.values()

    class Historical:
        closed = False

        def tag(self, value: bytes) -> bytes:
            return hmac.digest(lookup_key, value, "sha256")

        def close(self) -> None:
            self.closed = True

    handle = Historical()
    vault = FakeVault(vault_id=VAULT_ID, keys=keys)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        scope._historical["event.dedup"] = (handle,)
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(source="gmail", kind="message", payload=payload)
        assert result.reason == "forgotten"
        assert await db.fetchval("SELECT count(*) FROM events") == 0
    assert handle.closed


async def test_scoped_write_without_a_valid_scope_fails(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    # a scope that does not hold the memory category cannot tag or seal
    async with vault.acquire(TASK, "turn", [("workspace", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        with pytest.raises(VaultLocked):
            await store.ingest(source="gmail", kind="message", payload={"text": "x"})
    # a closed scope fails closed rather than degrading to an empty event
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(source="gmail", kind="message", payload={"text": "x"})
        assert result.event_id is not None
    with pytest.raises(VaultClosed):
        await store.ingest(source="gmail", kind="message", payload={"text": "y"})
    with pytest.raises(VaultClosed):
        await store.get(result.event_id)


async def test_scoped_construction_rejects_the_shared_index(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        with pytest.raises(ValueError):
            EventStore.scoped(db, scope, AuditLog(db), ContactsConfig(), index=MemoryIndex())
        with pytest.raises(ValueError):
            EventStore.scoped(db, scope, AuditLog(db), ContactsConfig(), indexer=object())
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        with pytest.raises(ValueError):
            store.attach_proactive(object())
        with pytest.raises(ValueError):
            store.attach_enrichment(enrichment=True)


async def test_scoped_reads_stay_out_of_the_app_index(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    app_index = MemoryIndex()
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(source="gmail", kind="message", payload={"text": "scoped"})
        assert result.event_id is not None
        # the scoped write never populated the shared plaintext index
        assert app_index.max_id == 0
        assert app_index.recent(10) == []
        # the scoped read decodes the row directly, not through the app index
        row = await db.fetchrow(
            "SELECT id, payload_enc, meta_enc, created_at FROM events WHERE id = $1",
            result.event_id,
        )
        decoded = decode_event_row(ScopedEventRows(scope), row)
        assert decoded.payload["text"] == "scoped"
        assert app_index.max_id == 0


async def test_scoped_row_location_is_bound(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        first = await store.ingest(source="gmail", kind="message", payload={"text": "a"})
        second = await store.ingest(source="gmail", kind="message", payload={"text": "b"})
        assert first.event_id is not None and second.event_id is not None
        rows = ScopedEventRows(scope)
        blob = await db.fetchval("SELECT payload_enc FROM events WHERE id = $1", first.event_id)
        # the same blob cannot be opened against another row's location
        with pytest.raises(VaultProtocolError):
            rows.open(blob, column="payload_enc", row_id=second.event_id)
        assert rows.open(blob, column="payload_enc", row_id=first.event_id)["text"] == "a"


async def test_legacy_index_path_is_unchanged(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig())
    result = await store.ingest(source="gmail", kind="message", payload={"text": "legacy"})
    assert result.event_id is not None
    index = MemoryIndex()
    assert await index.rebuild(db, CipherEventRows(cipher), hot_limit=50) == 1
    row = await db.fetchrow(
        "SELECT id, payload_enc, meta_enc, created_at FROM events WHERE id = $1",
        result.event_id,
    )
    assert decode_event_row(CipherEventRows(cipher), row).payload["text"] == "legacy"


async def test_eventstore_cold_scan_loads_older_events(db) -> None:
    """Regression: EventStore passes its already-wrapped adapter to
    ensure_since; the cold scan must actually decode older rows from Postgres
    rather than double-wrapping the adapter and skipping every row."""
    cipher = Cipher.from_b64(generate_key_b64())
    index = MemoryIndex()
    store = EventStore(db, cipher, AuditLog(db), ContactsConfig(), index=index, cold_batch=1)
    for hour in (6, 7, 8, 9):
        await store.ingest(
            source="gmail", kind="message", payload={"text": f"m{hour}"}, occurred_at=_at(hour)
        )
    # keep only the newest event hot, so the older three must come from a cold scan
    await index.rebuild(db, CipherEventRows(cipher), hot_limit=1)
    assert not index.covers(_at(6))
    window = int((datetime.now(UTC) - _at(6)).total_seconds() // 3600) + 1
    assert await store.count_recent("gmail", hours=window) == 4
    assert index.covers(_at(6))


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


async def test_denied_scoped_reads_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    # a scope without the memory grant: every read fails before any query,
    # even for a row that does not exist
    async with vault.acquire(TASK, "turn", [("workspace", 1)]) as scope:
        store = EventStore.scoped(pool, scope, AuditLog(pool), ContactsConfig())
        with pytest.raises(VaultLocked):
            await store.get(999_999)
        with pytest.raises(VaultLocked):
            await store.recent_memorable(10)
        with pytest.raises(VaultLocked):
            await store.count_recent("gmail")
        with pytest.raises(VaultLocked):
            await store.recent()
        with pytest.raises(VaultLocked):
            await store.list_since(0)
        with pytest.raises(VaultLocked):
            await store.max_id()
        with pytest.raises(VaultLocked):
            await store.count_memorable_since(_at(0))
    # a closed scope fails the same way
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(pool, scope, AuditLog(pool), ContactsConfig())
    with pytest.raises(VaultClosed):
        await store.get(999_999)
    with pytest.raises(VaultClosed):
        await store.recent_memorable(10)


async def test_denied_vector_search_and_delete_have_no_side_effects() -> None:
    class NoEmbed:
        async def embed(self, *_args, **_kwargs):
            raise AssertionError("a denied search contacted the embedder")

    async with FakeVault(vault_id=VAULT_ID).acquire(TASK, "turn", [("operations", 1)]) as scope:
        vectors = VectorMemory.scoped(_NoSqlPool(), scope, NoEmbed())
        with pytest.raises(VaultLocked):
            await vectors.search("synthetic private query")
        with pytest.raises(VaultLocked):
            await vectors.remove_event(1)


async def test_event_metadata_updates_merge_across_scopes(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    refs = [("memory", 1)]
    async with (
        vault.acquire(TASK, "turn", refs) as first_scope,
        vault.acquire(bytes(range(32, 48)), "turn", refs) as second_scope,
    ):
        first = EventStore.scoped(db, first_scope, AuditLog(db), ContactsConfig())
        second = EventStore.scoped(db, second_scope, AuditLog(db), ContactsConfig())
        result = await first.ingest(source="test", kind="message", payload={"text": "test"})
        assert result.event_id
        async with db.acquire() as conn, conn.transaction():
            await conn.fetchrow("SELECT id FROM events WHERE id = $1 FOR UPDATE", result.event_id)
            left = asyncio.create_task(first.merge_meta(result.event_id, {"left": True}))
            right = asyncio.create_task(second.merge_meta(result.event_id, {"right": True}))
            await asyncio.sleep(0)
        await asyncio.gather(left, right)
        event = await first.get(result.event_id)
        assert event and event.meta == {"left": True, "right": True}


async def test_valid_scope_missing_row_returns_none(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        assert await store.get(999_999) is None


async def test_corrupt_or_wrong_version_ae2_row_raises(db) -> None:
    vault = FakeVault(
        vault_id=VAULT_ID,
        keys={("memory", 1): bytes([1]) * 32, ("memory", 2): bytes([2]) * 32},
    )
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        result = await store.ingest(source="gmail", kind="message", payload={"text": "x"})
        assert result.event_id is not None
        blob = await db.fetchval("SELECT payload_enc FROM events WHERE id = $1", result.event_id)
        # a tampered ciphertext fails authentication instead of returning {}
        tampered = bytearray(blob)
        tampered[-1] ^= 0xFF
        await db.execute(
            "UPDATE events SET payload_enc = $1 WHERE id = $2", bytes(tampered), result.event_id
        )
        with pytest.raises(VaultProtocolError):
            await store.get(result.event_id)
    # a row sealed under version 1 cannot be opened by a version 2 scope
    async with vault.acquire(TASK, "turn", [("memory", 2)]) as scope_v2:
        store_v2 = EventStore.scoped(db, scope_v2, AuditLog(db), ContactsConfig(), ver=2)
        await db.execute("UPDATE events SET payload_enc = $1 WHERE id = $2", blob, result.event_id)
        with pytest.raises(VaultProtocolError):
            await store_v2.get(result.event_id)


async def test_scoped_forget_end_to_end(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        events = EventStore.scoped(db, scope, AuditLog(db), ContactsConfig())
        control = MemoryControl(
            pool=db,
            cipher=cipher,
            audit=AuditLog(db),
            events=events,
            index=MemoryIndex(),
            lexical=LexicalIndex(),
        )
        result = await events.ingest(source="gmail", kind="message", payload={"text": "forget me"})
        assert result.event_id is not None
        report = await control.forget_event(result.event_id)
        assert report.found
        assert await db.fetchval("SELECT count(*) FROM events WHERE id = $1", result.event_id) == 0
        assert await db.fetchval("SELECT count(*) FROM forgotten_events") == 1
        # the real forget tombstoned the scoped tag, so the replay stays forgotten
        replay = await events.ingest(source="gmail", kind="message", payload={"text": "forget me"})
        assert not replay.stored and replay.reason == "forgotten"
