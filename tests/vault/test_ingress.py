from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from test_lease_http import GRANT, http, pending, until
from test_lease_http import paired as paired_fixture

from aether.config import ContactsConfig
from aether.db_resilience import DatabaseUnavailable
from aether.memory.events import EventStore
from aether.vault.errors import VaultProtocolError
from aether.vault.ingress import Ingress, TransportDelivery
from aether.vault.keyed_work import TransactionPool
from aether.vault.runtime import VaultRuntime

pytestmark = pytest.mark.integration
paired = paired_fixture


def delivery(byte: bytes, length: int) -> TransportDelivery:
    return TransportDelivery(byte * 32, {"source": "synthetic", "source_id": byte.hex()}, length)


async def test_unauthenticated_accept_does_not_read_stream(db):
    ingress = Ingress(db, quota_bytes=4096)

    async def authenticate():
        raise VaultProtocolError("unauthorized")

    async def body():
        pytest.fail("unauthenticated body was consumed")
        yield b""

    with pytest.raises(VaultProtocolError, match="unauthorized"):
        await ingress.accept(body(), authenticate)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_queue") == 0


async def test_storage_outage_refuses_without_consuming_the_delivery(db, monkeypatch):
    ingress = Ingress(db, quota_bytes=4096)

    @asynccontextmanager
    async def offline():
        raise DatabaseUnavailable("synthetic outage")
        yield

    async def authenticate():
        return delivery(b"x", 1)

    async def body():
        pytest.fail("body was consumed after a storage failure")
        yield b"x"

    monkeypatch.setattr(ingress._store, "accepting", offline)
    with pytest.raises(VaultProtocolError, match="transient"):
        await ingress.accept(body(), authenticate)


async def test_locked_accept_replays_receipt_without_reopening_body(db, paired, tmp_path):
    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    ingress = Ingress(db, quota_bytes=4 * 1024 * 1024)
    canary = b"synthetic-attachment-canary-" * 50000

    async def authenticate():
        return delivery(b"a", len(canary))

    async def body():
        yield canary

    async def unreadable():
        pytest.fail("committed retry reopened the body")
        yield b""

    receipt = await ingress.accept(body(), authenticate)
    assert receipt == await Ingress(db, quota_bytes=4 * 1024 * 1024).accept(
        unreadable(), authenticate
    )
    parts = await db.fetch("SELECT ciphertext_enc FROM vault_ingress_part")
    assert len(parts) > 2
    assert all(b"synthetic-attachment-canary" not in row[0] for row in parts)
    assert not list(tmp_path.iterdir())


async def test_partial_accept_rolls_back_and_concurrent_retry_has_one_receipt(db, paired):
    ingress = Ingress(db, quota_bytes=1024 * 1024)
    body_bytes = b"x" * (300 * 1024)

    async def authenticate():
        return delivery(b"b", len(body_bytes))

    async def interrupted():
        yield body_bytes[:-1]
        raise ConnectionError("synthetic disconnect")

    with pytest.raises(ConnectionError, match="synthetic disconnect"):
        await ingress.accept(interrupted(), authenticate)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_queue") == 0
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_part") == 0

    async def short():
        yield body_bytes[:-1]

    with pytest.raises(VaultProtocolError, match="transient"):
        await ingress.accept(short(), authenticate)

    async def body():
        yield body_bytes

    a, b = await asyncio.gather(
        ingress.accept(body(), authenticate), ingress.accept(body(), authenticate)
    )
    assert a == b
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_queue") == 1


async def test_quota_refusal_leaves_no_partial_delivery(db, paired):
    ingress = Ingress(db, quota_bytes=512)

    async def authenticate():
        return delivery(b"c", 1024)

    async def body():
        yield b"x" * 1024

    with pytest.raises(VaultProtocolError, match="transient"):
        await ingress.accept(body(), authenticate)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_queue") == 0


async def test_quota_is_shared_by_concurrent_acceptors(db, paired):
    async def run(byte):
        async def authenticate():
            return delivery(byte, 512)

        async def body():
            yield byte * 512

        return await Ingress(db, quota_bytes=1500).accept(body(), authenticate)

    results = await asyncio.gather(run(b"g"), run(b"h"), return_exceptions=True)
    assert len([value for value in results if isinstance(value, bytes)]) == 1
    refusal = next(value for value in results if isinstance(value, VaultProtocolError))
    assert refusal.code == "transient"
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 1


@pytest.mark.parametrize("point", ["reserve", "chunk", "end", "receipt"])
async def test_accept_rolls_back_each_uncommitted_write(db, paired, monkeypatch, point):
    ingress = Ingress(db, quota_bytes=65536)
    method = {
        "reserve": "reserve_ingress",
        "chunk": "write_ingress_part",
        "end": "write_ingress_part",
        "receipt": "complete_ingress",
    }[point]
    original = getattr(ingress._store, method)

    async def interrupt(*args):
        await original(*args)
        if point not in ("chunk", "end") or args[-2] == point:
            raise RuntimeError("synthetic interrupted write")

    async def authenticate():
        return delivery(b"y", 4)

    async def body():
        yield b"test"

    with monkeypatch.context() as patch:
        patch.setattr(ingress._store, method, interrupt)
        with pytest.raises(RuntimeError, match="synthetic interrupted write"):
            await ingress.accept(body(), authenticate)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_queue") == 0
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_part") == 0
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 0
    assert await ingress.accept(body(), authenticate)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 1


async def test_drain_retries_in_commit_order_and_quarantines_encrypted_poison(db, paired):
    store, signer, phone = paired
    ingress = Ingress(db, quota_bytes=65536)

    async def accept(byte):
        async def authenticate():
            return delivery(byte, 1024)

        async def body():
            yield byte * 1024

        return await ingress.accept(body(), authenticate)

    first, second, third = [await accept(byte) for byte in (b"d", b"e", b"f")]
    await db.execute(
        "UPDATE vault_ingress_part SET ciphertext_enc = $1 WHERE queue_id = $2 AND part = 0",
        b"corrupt",
        second,
    )
    received = []

    async def apply(conn, scope, event, stream):
        body = b"".join([chunk async for chunk in stream])
        events = EventStore.scoped(TransactionPool(conn), scope, None, ContactsConfig())
        result = await events.ingest(
            source=event["source"],
            kind="note",
            payload={"text": body.decode("ascii")},
            dedup_key=event["source_id"],
        )
        assert result.stored
        received.append((event["source_id"], body))

    async def crash(conn, scope, event, stream):
        await apply(conn, scope, event, stream)
        raise RuntimeError("synthetic crash")

    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)

        async def drain(queue_id, action):
            async def work():
                async with service.work(
                    operation="drain",
                    use_case="ingress.drain",
                    refs=[("ingress", 1), ("memory", 1)],
                    writes={("memory", 1): 2},
                    ttl=10,
                ) as task:
                    return await ingress.drain(task, queue_id, action)

            task = asyncio.create_task(work())
            proof = await pending(client)
            assert (
                await client.post(GRANT, json=phone.issue(proof, now=int(time.time())).envelope)
            ).status_code == 204
            return await task

        assert await drain(second, apply) == "waiting"
        with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="synthetic crash")):
            await drain(first, crash)
        assert await db.fetchval("SELECT count(*) FROM events") == 0
        assert await drain(first, apply) == "drained"
        assert await accept(b"d") == first
        assert await drain(second, apply) == "quarantined"
        assert await drain(third, apply) == "drained"

        fourth = await accept(b"i")
        staged = asyncio.Event()

        async def pause(conn, scope, event, stream):
            await apply(conn, scope, event, stream)
            staged.set()
            await asyncio.Event().wait()

        running = asyncio.create_task(drain(fourth, pause))
        try:
            await asyncio.wait_for(staged.wait(), timeout=5)
            installation = service.installation
            assert await runtime.apply_verified_control(
                vault_id=installation.vault_id,
                epoch=installation.epoch,
                generation=installation.generation,
                state="locked",
            )
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert (
            await db.fetchval("SELECT state FROM vault_ingress_receipt WHERE queue_id = $1", fourth)
            == "queued"
        )
    assert received == [
        ("64", b"d" * 1024),
        ("64", b"d" * 1024),
        ("66", b"f" * 1024),
        ("69", b"i" * 1024),
    ]
    assert await db.fetchval("SELECT count(*) FROM events") == 2
    parts = await db.fetch(
        "SELECT ciphertext_enc FROM vault_ingress_part WHERE queue_id = $1", second
    )
    assert parts
    assert all(b"e" * 32 not in row[0] for row in parts)


async def test_crash_after_drain_settlement_rolls_back_destination_and_queue(
    db, paired, monkeypatch
):
    store, signer, phone = paired
    ingress = Ingress(db, quota_bytes=65536)

    async def authenticate():
        return delivery(b"z", 0)

    async def body():
        yield b""

    receipt = await ingress.accept(body(), authenticate)
    original = ingress._store.settle_ingress

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic settlement crash")

    async def write(conn, scope, event, stream):
        events = EventStore.scoped(TransactionPool(conn), scope, None, ContactsConfig())
        await events.ingest(
            source=event["source"],
            kind="note",
            payload={"text": "synthetic effect"},
            dedup_key=event["source_id"],
        )

    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)

        async def drain():
            async def work():
                async with service.work(
                    operation="drain",
                    use_case="ingress.drain",
                    refs=[("ingress", 1), ("memory", 1)],
                    writes={("memory", 1): 2},
                    ttl=10,
                ) as task:
                    return await ingress.drain(task, receipt, write)

            running = asyncio.create_task(work())
            proof = await pending(client)
            assert (
                await client.post(GRANT, json=phone.issue(proof, now=int(time.time())).envelope)
            ).status_code == 204
            return await running

        with monkeypatch.context() as patch:
            patch.setattr(ingress._store, "settle_ingress", crash)
            with pytest.RaisesGroup(
                pytest.RaisesExc(RuntimeError, match="synthetic settlement crash")
            ):
                await drain()
        assert await db.fetchval("SELECT count(*) FROM events") == 0
        assert (
            await db.fetchval(
                "SELECT state FROM vault_ingress_receipt WHERE queue_id = $1", receipt
            )
            == "queued"
        )
        assert await drain() == "drained"
        assert await db.fetchval("SELECT count(*) FROM events") == 1


async def test_production_drain_quarantines_invalid_source_and_advances(db, paired, tmp_path):
    from aether.vault.ingress import sealed_destination

    store, signer, phone = paired
    config = tmp_path / "drain.yaml"
    config.write_text("{}")
    ingress = Ingress(db, quota_bytes=65536, apply=sealed_destination(str(config)))

    async def accept(source, token, source_id="numbered-event"):
        async def authenticate():
            return TransportDelivery(
                token * 32,
                {
                    "source": source,
                    "source_id": source_id,
                    "chat_ref": "synthetic-chat",
                    "message_id": "17",
                    "sender": {"platform": "telegram", "handle": "synthetic-sender"},
                },
                0,
            )

        async def body():
            yield b""

        return await ingress.accept(body(), authenticate)

    bad = await accept("mode", b"a")
    await accept("telegram", b"b")
    await accept("telegram", b"c")
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)

        async def drain():
            running = asyncio.create_task(ingress.drain_pending(service))
            proof = await pending(client)
            issued = phone.issue(proof, now=int(time.time())).envelope
            assert (await client.post(GRANT, json=issued)).status_code == 204
            return await running

        assert await drain() == "quarantined"
        assert await drain() == "drained"
        assert await drain() == "drained"

    assert await db.fetchval("SELECT count(*) FROM events") == 1
    assert await db.fetchval("SELECT count(*) FROM inbound_messages") == 1
    ciphertext = await db.fetchval(
        "SELECT ciphertext_enc FROM vault_ingress_part WHERE queue_id = $1", bad
    )
    assert ciphertext and b"synthetic-sender" not in ciphertext


@pytest.mark.parametrize("source", ["telegram", "discord"])
async def test_provider_backlog_survives_an_interrupted_drain(
    db, paired, tmp_path, monkeypatch, source
):
    from hashlib import sha256
    from types import SimpleNamespace

    from aether.config import MediaConfig
    from aether.media.store import MediaStore
    from aether.vault.discord_ingress import capture_history
    from aether.vault.ingress import sealed_destination
    from aether.vault.telegram_ingress import capture_page

    store, signer, phone = paired
    config = tmp_path / "capture.yaml"
    config.write_text(
        f"contacts:\n  mode: enforce\n  {source}:\n    allowlist:\n"
        f'      - {{platform: {source}, handle: "existing_owner"}}\n'
    )
    owner = SimpleNamespace(
        id=42, bot=False, is_bot=False, username="existing_owner", name="existing_owner"
    )
    channel = SimpleNamespace(id=42, type="private")
    ledger = {}
    expired = {12}

    def message(number):
        data = ledger[number]

        async def download():
            if number in expired:
                raise OSError("expired attachment URL")
            return data

        async def get_file():
            return SimpleNamespace(download_as_bytearray=download)

        attachment = SimpleNamespace(filename="note", size=len(data), read=download)
        document = SimpleNamespace(file_name="note", file_size=len(data), get_file=get_file)
        body = SimpleNamespace(
            id=number,
            message_id=number,
            text=f"Numbered event {number}",
            content=f"Numbered event {number}",
            date=None,
            created_at=None,
            chat=channel,
            author=owner,
            attachments=[attachment],
            document=document,
            effective_attachment=document,
            api_kwargs={},
        )
        return body

    async def updates(*, offset, **kwargs):
        return [
            SimpleNamespace(update_id=number, message=message(number), effective_user=owner)
            for number in sorted(ledger)
            if offset is None or number >= offset
        ]

    async def history(*, limit, after=None, oldest_first=False):
        for number in sorted(ledger, reverse=not oldest_first):
            if after is None or number > after.id:
                yield message(number)

    channel.history = history
    bot = SimpleNamespace(id=100, get_updates=updates)

    def new_ingress():
        return Ingress(db, quota_bytes=65536, apply=sealed_destination(str(config)))

    async def capture(ingress, checkpoint=None):
        if source == "telegram":
            return await capture_page(bot, ingress, frozenset({owner.id}), b"fixture", checkpoint)
        return await capture_history(channel, ingress, owner.id, b"fixture", checkpoint)

    # Both senders have a backlog before any host worker or phone grant is live.
    ledger.update({number: f"attachment {number}".encode() for number in (11, 12)})
    with pytest.raises(OSError, match="expired attachment"):
        await capture(new_ingress())
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 1

    ledger[13] = b"attachment 13"
    expired.clear()
    ingress = new_ingress()
    checkpoint = await capture(ingress)
    assert await db.fetchval("SELECT count(*) FROM vault_ingress_receipt") == 3
    await capture(new_ingress())
    await capture(ingress, checkpoint)

    async def crash(*args, **kwargs):
        raise RuntimeError("interrupted drain")

    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)

        async def granted(coro):
            running = asyncio.create_task(coro)
            try:
                proof = await pending(client)
                issued = phone.issue(proof, now=int(time.time())).envelope
                assert (await client.post(GRANT, json=issued)).status_code == 204
                return await running
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

        with monkeypatch.context() as patch:
            patch.setattr(ingress._store, "settle_ingress", crash)
            with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="interrupted drain")):
                await granted(ingress.drain_pending(service))
        assert await db.fetchval("SELECT count(*) FROM events") == 0
        assert await db.fetchval("SELECT count(*) FROM media_blobs") == 0

        restarted = new_ingress()
        for _ in ledger:
            assert await granted(restarted.drain_pending(service)) == "drained"
        assert await restarted.drain_pending(service) == "absent"

        async def read_effects():
            async with service.work(
                operation="drain",
                use_case="ingress.drain",
                refs=[("ingress", 1), ("memory", 1), ("media", 1)],
                ttl=10,
            ) as work:
                events = EventStore.scoped(db, work.scope, None, ContactsConfig())
                media = MediaStore.scoped(db, work.scope, MediaConfig())
                return [
                    (
                        event.payload["source_id"],
                        sha256((await media.get(event.payload["media_id"])).data).digest(),
                    )
                    for event in await events.list_since(0)
                ]

        prefix = "100:" if source == "telegram" else "discord:42:"
        assert await granted(read_effects()) == [
            (f"{prefix}{number}", sha256(data).digest()) for number, data in ledger.items()
        ]
    assert await db.fetchval("SELECT count(*) FROM inbound_messages") == len(ledger)
