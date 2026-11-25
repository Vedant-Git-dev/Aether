from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from aether.memory.storage import schema_violations
from aether.vault.errors import VaultProtocolError
from aether.vault.policy import policy
from aether.vault.scope import VaultExhausted
from aether.vault.store import VaultStore, queue_is_complete

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
DEVICE_ID = bytes(range(16, 32))
DIGEST1 = b"\x01" * 32
DIGEST2 = b"\x02" * 32
MANIFEST1 = b'{"v":1,"type":"manifest","revision":1}'
MANIFEST2 = b'{"v":1,"type":"manifest","revision":2}'
PINS = b'{"origin":"https://vault.example.test"}'


def now() -> datetime:
    # Resolve at call time: a full-suite run can outlive an import-time value.
    return datetime.now(UTC)


async def seed(store: VaultStore) -> None:
    await store.create(
        vault_id=VAULT_ID,
        device_id=DEVICE_ID,
        digest=DIGEST1,
        manifest=MANIFEST1,
        pins=PINS,
    )


async def test_serving_boot_excludes_and_fences_the_previous_generation(db) -> None:
    store = VaultStore(db)
    await seed(store)
    assert await store.set_state(VAULT_ID, "available", expected_epoch=1, expected_generation=1)
    async with store.serving_boot() as first:
        state = await store.load(VAULT_ID)
        fence = {"vault_id": VAULT_ID, "generation": 3, "epoch": 2, "manifest_digest": DIGEST1}
        assert state["generation"] == fence["generation"]
        with pytest.raises(VaultProtocolError, match="^unavailable$"):
            async with store.serving_boot():
                pytest.fail("two sessions held serving leadership")
        async with first.transaction(**fence):
            pass
    assert first.lost.is_set()

    async with store.serving_boot() as second:
        assert (await store.load(VAULT_ID))["generation"] == 4
        for boot, snapshot in ((first, fence | {"generation": 4}), (second, fence)):
            with pytest.raises(VaultProtocolError, match="^stale_generation$"):
                async with boot.transaction(**snapshot):
                    pytest.fail("stale boot or snapshot opened a write transaction")
        async with second.transaction(**(fence | {"generation": 4})):
            pass


async def test_serving_session_loss_rolls_back_uncommitted_effects(db) -> None:
    store = VaultStore(db)
    await seed(store)
    assert await store.set_state(VAULT_ID, "available", expected_epoch=1, expected_generation=1)
    ticket_hash = b"\x99" * 32
    async with store.serving_boot() as boot:
        with pytest.raises(VaultProtocolError, match="^stale_generation$"):
            async with boot.transaction(
                vault_id=VAULT_ID, generation=3, epoch=2, manifest_digest=DIGEST1
            ) as conn:
                await conn.execute(
                    "INSERT INTO vault_ticket (ticket_hash, vault_id, expires_at) VALUES ($1,$2,$3)",
                    ticket_hash,
                    VAULT_ID,
                    now() + timedelta(minutes=1),
                )
                pid = boot._connection.get_server_pid()
                assert await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
                await asyncio.wait_for(boot.lost.wait(), timeout=5)
    assert await store.consume_ticket(ticket_hash) is None


async def test_create_and_load(db) -> None:
    store = VaultStore(db)
    await seed(store)
    row = await store.load(VAULT_ID)
    assert row is not None
    assert row["device_id"] == DEVICE_ID
    assert row["epoch"] == 1
    assert row["revision"] == 1
    assert row["digest"] == DIGEST1
    assert row["manifest"] == MANIFEST1
    assert row["pins"] == PINS
    assert row["state"] == "locked"
    assert row["generation"] == 1


async def test_manifest_cas_accepts_one_writer(db) -> None:
    store = VaultStore(db)
    await seed(store)
    assert await store.publish_manifest(
        vault_id=VAULT_ID,
        expected_revision=1,
        expected_digest=DIGEST1,
        expected_epoch=1,
        expected_generation=1,
        revision=2,
        digest=DIGEST2,
        previous_digest=DIGEST1,
        manifest=MANIFEST2,
    )
    assert not await store.publish_manifest(
        vault_id=VAULT_ID,
        expected_revision=1,
        expected_digest=DIGEST1,
        expected_epoch=1,
        expected_generation=1,
        revision=2,
        digest=DIGEST2,
        previous_digest=DIGEST1,
        manifest=MANIFEST2,
    )
    row = await store.load(VAULT_ID)
    assert row is not None
    assert row["revision"] == 2
    assert row["digest"] == DIGEST2
    assert row["generation"] == 2


async def test_concurrent_manifest_cas_has_one_winner(db) -> None:
    store = VaultStore(db)
    await seed(store)
    results = await asyncio.gather(
        *[
            store.publish_manifest(
                vault_id=VAULT_ID,
                expected_revision=1,
                expected_digest=DIGEST1,
                expected_epoch=1,
                expected_generation=1,
                revision=2,
                digest=DIGEST2,
                previous_digest=DIGEST1,
                manifest=MANIFEST2,
            )
            for _ in range(4)
        ]
    )
    assert sum(results) == 1


async def test_set_state_advances_the_epoch_and_generation(db) -> None:
    store = VaultStore(db)
    await seed(store)
    assert await store.set_state(VAULT_ID, "available", expected_epoch=1, expected_generation=1)
    row = await store.load(VAULT_ID)
    assert row is not None
    assert row["state"] == "available"
    assert row["epoch"] == 2
    assert row["generation"] == 2
    assert not await store.set_state(
        b"\xff" * 16, "locked", expected_epoch=1, expected_generation=1
    )
    assert await store.set_state(VAULT_ID, "locked", expected_epoch=2, expected_generation=2)
    assert not await store.set_state(VAULT_ID, "available", expected_epoch=1, expected_generation=1)
    assert not await store.set_state(VAULT_ID, "available", expected_epoch=3, expected_generation=2)
    row = await store.load(VAULT_ID)
    assert (row["state"], row["epoch"], row["generation"]) == ("locked", 3, 3)
    assert await store.set_state(VAULT_ID, "revoked", expected_epoch=3, expected_generation=3)
    assert not await store.set_state(VAULT_ID, "available", expected_epoch=4, expected_generation=4)


async def test_ticket_is_single_use(db) -> None:
    store = VaultStore(db)
    ticket = b"\x03" * 32
    await store.create_ticket(
        ticket_hash=ticket, vault_id=VAULT_ID, expires_at=now() + timedelta(minutes=5)
    )
    assert await store.consume_ticket(ticket) == VAULT_ID
    assert await store.consume_ticket(ticket) is None


async def test_concurrent_ticket_consumption_has_one_winner(db) -> None:
    store = VaultStore(db)
    ticket = b"\x04" * 32
    await store.create_ticket(
        ticket_hash=ticket, vault_id=VAULT_ID, expires_at=now() + timedelta(minutes=5)
    )
    results = await asyncio.gather(*[store.consume_ticket(ticket) for _ in range(4)])
    assert sum(result is not None for result in results) == 1
    assert VAULT_ID in results


async def test_expired_ticket_is_not_consumed(db) -> None:
    store = VaultStore(db)
    ticket = b"\x05" * 32
    await store.create_ticket(
        ticket_hash=ticket, vault_id=VAULT_ID, expires_at=now() - timedelta(minutes=1)
    )
    assert await store.consume_ticket(ticket) is None


async def test_challenge_is_single_use(db) -> None:
    store = VaultStore(db)
    challenge = b"\x06" * 32
    await store.create_challenge(
        challenge_hash=challenge,
        vault_id=VAULT_ID,
        device_id=DEVICE_ID,
        purpose="device_session",
        expires_at=now() + timedelta(seconds=60),
    )
    assert (
        await store.consume_challenge(
            challenge, vault_id=VAULT_ID, device_id=DEVICE_ID, purpose="restore"
        )
        is None
    )
    attempts = await asyncio.gather(
        *(
            store.consume_challenge(
                challenge, vault_id=VAULT_ID, device_id=DEVICE_ID, purpose="device_session"
            )
            for _ in range(4)
        )
    )
    assert sum(result is not None for result in attempts) == 1
    row = next(result for result in attempts if result is not None)
    assert row is not None
    assert row["vault_id"] == VAULT_ID
    assert row["device_id"] == DEVICE_ID
    assert row["purpose"] == "device_session"
    assert (
        await store.consume_challenge(
            challenge, vault_id=VAULT_ID, device_id=DEVICE_ID, purpose="device_session"
        )
        is None
    )


async def test_session_revocation_is_idempotent(db) -> None:
    store = VaultStore(db)
    token = b"\x07" * 32
    await store.create_session(
        session_hash=token,
        vault_id=VAULT_ID,
        device_id=DEVICE_ID,
        kind="device",
        epoch=2,
        expires_at=now() + timedelta(minutes=15),
    )
    assert await store.revoke_session(token)
    assert not await store.revoke_session(token)


async def test_queue_reservation_parts_and_completion(db) -> None:
    store = VaultStore(db)
    queue_id = b"\x08" * 16
    await store.reserve_queue(queue_id=queue_id, vault_id=VAULT_ID, ingress_ver=1)
    row, parts = await store.queue_group(queue_id)
    assert row is not None and not row["complete"]
    assert parts == []
    assert not queue_is_complete(row, parts)

    assert not await store.append_part(queue_id=queue_id, part=1, kind="end", ciphertext=b"early")
    assert await store.append_part(queue_id=queue_id, part=0, kind="chunk", ciphertext=b"chunk-0")
    assert not await store.seal_queue(queue_id, b"mixed-record")
    assert not await store.append_part(
        queue_id=queue_id, part=0, kind="chunk", ciphertext=b"chunk-0"
    )
    row, parts = await store.queue_group(queue_id)
    assert row is not None and not row["complete"]
    assert not queue_is_complete(row, parts)

    assert await store.append_part(queue_id=queue_id, part=1, kind="end", ciphertext=b"end")
    row, parts = await store.queue_group(queue_id)
    assert row is not None and row["complete"]
    assert queue_is_complete(row, parts)
    assert [part["part"] for part in parts] == [0, 1]
    assert not await store.append_part(queue_id=queue_id, part=2, kind="chunk", ciphertext=b"late")
    assert not await store.append_part(queue_id=queue_id, part=2, kind="end", ciphertext=b"late")
    invalid_parts = [{"part": 0, "kind": "end", "ciphertext": b"wrong"}, parts[1]]
    assert not queue_is_complete(row, invalid_parts)


async def test_single_record_queue_is_sealed_once(db) -> None:
    store = VaultStore(db)
    queue_id = b"\x09" * 16
    await store.reserve_queue(queue_id=queue_id, vault_id=VAULT_ID, ingress_ver=1)
    assert await store.seal_queue(queue_id, b"sealed-envelope")
    assert not await store.seal_queue(queue_id, b"second-envelope")
    assert not await store.append_part(queue_id=queue_id, part=0, kind="end", ciphertext=b"late")
    row, parts = await store.queue_group(queue_id)
    assert row is not None
    assert row["ciphertext"] == b"sealed-envelope"
    assert queue_is_complete(row, parts)


async def test_migration_journal_upserts_and_reads_state(db) -> None:
    store = VaultStore(db)
    job_id = b"\x0a" * 16
    await store.journal(
        job_id=job_id,
        vault_id=VAULT_ID,
        table_name="events",
        stable_id=b"\x12" * 32,
        state="pending",
    )
    await store.journal(
        job_id=job_id,
        vault_id=VAULT_ID,
        table_name="events",
        stable_id=b"\x12" * 32,
        state="done",
        detail_enc=b"sealed-detail",
    )
    row = await store.journal_entry(
        vault_id=VAULT_ID, job_id=job_id, table_name="events", stable_id=b"\x12" * 32
    )
    assert row is not None
    assert row["state"] == "done"
    assert row["detail_enc"] == b"sealed-detail"


async def test_schema_audit_accepts_the_vault_tables(db) -> None:
    assert await schema_violations(db) == []


async def test_single_installation_has_one_winner(db) -> None:
    store = VaultStore(db)
    attempts = await asyncio.gather(
        seed(store),
        store.create(
            vault_id=b"\x12" * 16,
            device_id=DEVICE_ID,
            digest=DIGEST1,
            manifest=MANIFEST1,
            pins=PINS,
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in attempts) == 1
    assert sum(isinstance(result, asyncpg.UniqueViolationError) for result in attempts) == 1


async def test_manifest_continuity_and_state_fence(db) -> None:
    store = VaultStore(db)
    await seed(store)
    update = {
        "vault_id": VAULT_ID,
        "expected_revision": 1,
        "expected_digest": DIGEST1,
        "expected_epoch": 1,
        "expected_generation": 1,
        "digest": DIGEST2,
        "manifest": MANIFEST2,
    }
    assert not await store.publish_manifest(**update, revision=3, previous_digest=DIGEST1)
    assert not await store.publish_manifest(**update, revision=2, previous_digest=DIGEST2)
    assert await store.set_state(VAULT_ID, "locked", expected_epoch=1, expected_generation=1)
    assert not await store.publish_manifest(**update, revision=2, previous_digest=DIGEST1)
    assert await db.fetchval("SELECT count(*) FROM vault_manifest") == 1


async def test_write_reservations_survive_handle_loss_and_competing_writers(db) -> None:
    await seed(VaultStore(db))
    ref = {"vault_id": VAULT_ID, "cat": "memory", "ver": 1}
    limit = policy().aes_gcm_operational_threshold
    await VaultStore(db).reserve_aead(**ref, count=limit - 1)
    attempts = await asyncio.gather(
        *(VaultStore(db).reserve_aead(**ref, count=1) for _ in range(4)), return_exceptions=True
    )
    assert sum(isinstance(item, VaultExhausted) for item in attempts) == 3
    reservation = next(item for item in attempts if not isinstance(item, Exception))
    reservation.spend()
    with pytest.raises(VaultExhausted):
        reservation.spend()
    with pytest.raises(VaultExhausted):
        await VaultStore(db).reserve_aead(**ref, count=1)
    rotated = await VaultStore(db).reserve_aead(**(ref | {"ver": 2}), count=1)
    rotated.spend()


async def test_single_and_chunked_queue_writers_cannot_mix(db) -> None:
    store = VaultStore(db)
    queue_id = b"\x13" * 16
    await store.reserve_queue(queue_id=queue_id, vault_id=VAULT_ID, ingress_ver=1)
    results = await asyncio.gather(
        store.seal_queue(queue_id, b"single"),
        store.append_part(queue_id=queue_id, part=0, kind="chunk", ciphertext=b"first"),
    )
    assert sum(results) == 1
    row, parts = await store.queue_group(queue_id)
    assert bool(row["ciphertext"]) != bool(parts)
