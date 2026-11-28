from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
import reference_wire as wire
from fastapi import FastAPI
from test_software_vault import SERVER_PRIVATE_RAW, new_vault

from aether.config import Settings
from aether.main import create_app
from aether.vault import native, native_grant, schema
from aether.vault.errors import VaultProtocolError
from aether.vault.http import router
from aether.vault.runtime import VaultRuntime
from aether.vault.scope import VaultClosed, VaultLocked
from aether.vault.signing import RequestSigner
from aether.vault.store import VaultStore

pytestmark = pytest.mark.integration
REF = ("memory", 1)
PENDING = schema.vault_path("pending")
GRANT = schema.vault_path("grant")
DENY = schema.vault_path("deny")


def headers(token: bytes) -> dict[str, str]:
    return {"Authorization": f"Bearer {schema.b64u_encode(token)}"}


@pytest.fixture
async def paired(db):
    try:
        native._sodium()
        native_grant._openssl()
    except VaultLocked as exc:
        pytest.skip(str(exc))
    phone = new_vault()
    signer = RequestSigner.from_scalar(schema.b64u_encode(SERVER_PRIVATE_RAW))
    store = VaultStore(db)
    await store.create(
        vault_id=phone.vault_id,
        device_id=phone.device_id,
        epoch=phone.epoch,
        digest=wire.body_digest(phone.manifest_body),
        manifest=schema.canonical_bytes(phone.manifest),
        pins=schema.canonical_bytes(
            {
                "origin": phone.origin,
                "phone_pub": schema.b64u_encode(phone.phone_public),
                "server_kid": signer.kid,
            }
        ),
        state="available",
    )
    now = datetime.now(UTC)
    for byte, kind, epoch, expiry in (
        (b"d", "device", phone.epoch, now + timedelta(minutes=5)),
        (b"p", "panel", phone.epoch, now + timedelta(minutes=5)),
        (b"e", "device", phone.epoch, now - timedelta(minutes=1)),
        (b"s", "device", phone.epoch - 1, now + timedelta(minutes=5)),
    ):
        await store.create_session(
            session_hash=sha256(byte * 32).digest(),
            vault_id=phone.vault_id,
            device_id=phone.device_id,
            kind=kind,
            epoch=epoch,
            expires_at=expiry,
        )
    return store, signer, phone


def http(runtime: VaultRuntime, *, authenticated: bool = True) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.vault_runtime = runtime
    app.include_router(router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://vault.example.test",
        headers=headers(b"d" * 32) if authenticated else None,
    )


async def until(fn: Callable):
    async with asyncio.timeout(5):
        while True:
            try:
                if result := fn():
                    return result
            except VaultProtocolError as exc:
                if exc.code != "unavailable":
                    raise
            await asyncio.sleep(0.01)


async def pending(client: httpx.AsyncClient) -> dict:
    async with asyncio.timeout(5):
        while True:
            response = await client.get(PENDING)
            if response.status_code == 200:
                assert response.headers["cache-control"] == "no-store"
                return response.json()
            assert response.status_code == 204
            await asyncio.sleep(0.01)


async def test_vault_http_authenticates_only_current_device_and_never_returns_grant(paired):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime, authenticated=False) as client:
        service = await until(runtime.serving)
        token = headers(b"d" * 32)
        for credentials in (
            {},
            {"x-aether-token": "legacy"},
            *(headers(byte * 32) for byte in (b"p", b"e", b"s")),
        ):
            response = await client.get(PENDING, headers=credentials)
            assert response.status_code == 401
            assert response.json() == {"code": "unauthorized"}
            assert response.headers["cache-control"] == "no-store"
        response = await client.get(PENDING, headers=token | {"Origin": "https://other.example"})
        assert response.status_code == 401
        assert (await client.get(PENDING, headers=token)).status_code == 204
        ticket = await service.begin(
            task=b"t" * 16, operation="turn", use_case="turn", refs=[REF], ttl=60
        )
        proof = (await client.get(PENDING, headers=token)).json()

        # A copied request signer can sign a different request and boot key,
        # but the honest server has no corresponding entry in its pending map.
        other_recipient = native_grant.NativeBootKey()
        try:
            forged = signer.sign_request(
                proof["body"]
                | {
                    "request": schema.b64u_encode(b"F" * 16),
                    "nonce": schema.b64u_encode(b"N" * 32),
                    "epk": schema.b64u_encode(other_recipient.public),
                }
            )
            forged_grant = phone.issue(forged, now=int(time.time())).envelope
            response = await client.post(GRANT, headers=token, json=forged_grant)
            assert response.status_code == 409
            assert response.json() == {"code": "conflict"}
        finally:
            other_recipient.close()

        response = await client.post(
            GRANT,
            headers=token | {"Content-Type": "application/json"},
            content=b'{"body":1,"body":2}',
        )
        assert response.status_code == 400
        assert response.json() == {"code": "duplicate_field"}
        grant = phone.issue(proof, now=int(time.time())).envelope
        for _ in range(2):
            response = await client.post(GRANT, headers=token, json=grant)
            assert response.status_code == 204
            assert response.content == b""
            assert response.headers["cache-control"] == "no-store"
        assert (await ticket.wait()).refs == (REF,)
        denied = await service.begin(
            task=b"n" * 16, operation="turn", use_case="turn", refs=[REF], ttl=60
        )
        body = (await client.get(PENDING, headers=token)).json()["body"]
        response = await client.post(
            DENY, headers=token, json=phone.deny(body, "locked", now=int(time.time()))
        )
        assert response.status_code == 204
        with pytest.raises(VaultProtocolError, match="^denied$"):
            await denied.wait()


async def test_application_lifespan_owns_serving_boot_and_mounts_vault_routes(paired, tmp_path):
    _, _, phone = paired
    config = tmp_path / "config.yaml"
    config.write_text(
        "llm:\n  provider: ollama\n  model: synthetic\n  embedding_model: off\nworkspace:\n  enabled: false\n",
        encoding="utf-8",
    )
    settings = Settings.without_env_file(
        database_url=os.environ["AETHER_TEST_DATABASE_URL"],
        dev_ephemeral_key=True,
        api_token="synthetic-panel-token",
        ollama_base_url="http://127.0.0.1:1",
        vault_request_signing_key=schema.b64u_encode(SERVER_PRIVATE_RAW),
        vault_pending_wait_seconds=0,
    )
    app = create_app(settings=settings, config_path=str(config))
    async with app.router.lifespan_context(app):
        service = await until(app.state.vault_runtime.serving)
        assert service.installation.vault_id == phone.vault_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=phone.origin
        ) as client:
            response = await client.get(PENDING, headers=headers(b"d" * 32))
            assert response.status_code == 204
    assert service.closed


async def test_rolling_boot_rejects_grant_bound_to_previous_boot(paired):
    store, signer, phone = paired
    second = VaultRuntime(store, signer)
    async with second:
        # Start the first worker with leadership already reserved for this boot.
        await asyncio.sleep(0)
        first_service = await until(second.serving)
        standby = VaultRuntime(store, signer)
        async with standby, http(standby) as second_http:
            await first_service.begin(
                task=b"t" * 16, operation="turn", use_case="turn", refs=[REF], ttl=60
            )
            old_request = schema.loads_strict(await first_service.pending())
            old = phone.issue(old_request, now=int(time.time())).envelope
            assert (await second_http.get(PENDING)).status_code == 503
            await second.__aexit__()
            promoted = await until(standby.serving)
            assert promoted.installation.generation > first_service.installation.generation
            response = await second_http.post(GRANT, json=old)
            assert response.status_code == 409
            assert response.json() == {"code": "stale_generation"}
            assert first_service.closed


async def test_pending_wait_wakes_for_work_and_revocation(paired):
    store, signer, _ = paired
    async with VaultRuntime(store, signer, pending_wait=4) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        polling = asyncio.create_task(client.get(PENDING))
        try:
            await until(runtime.pending_poll.locked)
            assert (await client.get(PENDING)).status_code == 409
            ticket = await service.begin(
                task=b"t" * 16,
                operation="turn",
                use_case="turn",
                refs=[REF],
                ttl=60,
            )
            response = await asyncio.wait_for(polling, 2)
            assert response.content == ticket.wire
            service.end(b"t" * 16)

            polling = asyncio.create_task(client.get(PENDING))
            await until(runtime.pending_poll.locked)
            assert await store.revoke_session(sha256(b"d" * 32).digest())
            response = await asyncio.wait_for(polling, 3)
            assert response.status_code == 401
            assert response.json() == {"code": "unauthorized"}
        finally:
            if not polling.done():
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)


async def test_progress_renews_task_and_pending_renewal_dies_with_owner(paired):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        started, progress = asyncio.Event(), asyncio.Event()
        scopes = []

        async def step():
            return None

        async def task():
            async with service.work(operation="turn", use_case="turn", refs=[REF], ttl=4) as work:
                scopes.append(work.scope)
                started.set()
                while True:
                    await progress.wait()
                    progress.clear()
                    await work.step(step)

        owner = asyncio.create_task(task())
        try:
            envelope = await pending(client)
            for _ in range(3):
                response = await client.post(
                    GRANT, json=phone.issue(envelope, now=int(time.time())).envelope
                )
                assert response.status_code == 204
                await asyncio.wait_for(started.wait(), 5)
                await asyncio.sleep(0.05)
                scopes[0].require(*REF)
                progress.set()
                next_envelope = await pending(client)
                assert next_envelope["body"]["task"] == envelope["body"]["task"]
                assert next_envelope["body"]["nonce"] != envelope["body"]["nonce"]
                envelope = next_envelope
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert scopes[0].closed
            response = await client.post(
                GRANT, json=phone.issue(envelope, now=int(time.time())).envelope
            )
            assert response.status_code == 409
        finally:
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_lock_fences_stalled_children_before_acknowledgement(paired, cancel_caller):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        scopes = []
        ticket_hash = sha256(b"rollback probe").digest()

        async def task():
            async with service.work(operation="turn", use_case="turn", refs=[REF], ttl=60) as work:
                scopes.append(work.scope)

                async def child():
                    async with work.transaction() as connection:
                        await connection.execute(
                            "INSERT INTO vault_ticket (ticket_hash, vault_id, expires_at)"
                            " VALUES ($1, $2, now() + interval '1 minute')",
                            ticket_hash,
                            phone.vault_id,
                        )
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            with pytest.raises(VaultClosed):
                                await work.dispatch(lambda: pytest.fail("dispatched after lock"))
                            cleaning.set()
                            await release.wait()

                work.start(child)

        owner = asyncio.create_task(task())
        control = None
        try:
            proof = await pending(client)
            assert (
                await client.post(GRANT, json=phone.issue(proof, now=int(time.time())).envelope)
            ).status_code == 204
            await asyncio.wait_for(entered.wait(), 5)
            item = service.installation
            control = asyncio.create_task(
                runtime.apply_verified_control(
                    vault_id=item.vault_id,
                    epoch=item.epoch,
                    generation=item.generation,
                    state="locked",
                )
            )
            await asyncio.wait_for(cleaning.wait(), 5)
            assert not control.done()
            assert scopes[0].closed
            assert (await client.get(PENDING)).status_code == 503
            transition = next(iter(runtime._transitions))
            if cancel_caller:
                control.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await control
                assert not transition.done()
            release.set()
            assert await asyncio.wait_for(asyncio.shield(transition), 5)
            if not cancel_caller:
                assert await control
            with pytest.raises(asyncio.CancelledError):
                await owner
            row = await store.load(item.vault_id)
            assert (row["state"], row["epoch"]) == ("locked", item.epoch + 1)
            assert (await client.get(PENDING)).status_code == 401
            assert await store.consume_ticket(ticket_hash) is None
        finally:
            release.set()
            if not owner.done():
                owner.cancel()
            await asyncio.gather(
                *(task for task in (owner, control) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize("change", ["epoch", "manifest"])
async def test_grant_cannot_publish_after_authority_changes(paired, monkeypatch, change):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        item = service.installation
        ticket = await service.begin(
            task=b"t" * 16,
            operation="turn",
            use_case="turn",
            refs=[REF],
            ttl=60,
            writes={REF: 1},
        )
        proof = schema.loads_strict(ticket.wire)
        grant = phone.issue(proof, now=int(time.time())).envelope
        pause, resume = asyncio.Event(), asyncio.Event()
        fence = service._fence
        fence_count = 0
        opened_slots = []
        open_slots = service._recipient.open_grant_slots

        def observe_open(body):
            slots = open_slots(body)
            opened_slots.extend(slots.values())
            return slots

        @asynccontextmanager
        async def paused_fence(**kwargs):
            nonlocal fence_count
            fence_count += 1
            current = fence_count
            async with fence(**kwargs) as conn:
                yield conn
            if current == 2:  # Reservation committed. Publication has yet to start.
                pause.set()
                await resume.wait()

        monkeypatch.setattr(service, "_fence", paused_fence)
        monkeypatch.setattr(service._recipient, "open_grant_slots", observe_open)
        callback = asyncio.create_task(client.post(GRANT, json=grant))
        try:
            await asyncio.wait_for(pause.wait(), 5)
            offline_store = VaultStore(store._pool)
            with pytest.raises(VaultProtocolError, match="^unavailable$"):
                await offline_store.set_state(
                    item.vault_id,
                    "locked",
                    expected_epoch=item.epoch,
                    expected_generation=item.generation,
                )
            if change == "epoch":
                assert await runtime.apply_verified_control(
                    vault_id=item.vault_id,
                    epoch=item.epoch,
                    generation=item.generation,
                    state="locked",
                )
            else:
                body = phone.manifest_body | {
                    "revision": 2,
                    "previous_digest": schema.b64u_encode(item.digest),
                }
                signed = wire.sign_body(
                    body, wire.ec_private_key(phone.phone_private), alg="ES256", kid=phone.phone_kid
                )
                assert await runtime.publish_verified_manifest(
                    vault_id=item.vault_id,
                    expected_epoch=item.epoch,
                    expected_generation=item.generation,
                    expected_revision=1,
                    expected_digest=item.digest,
                    revision=2,
                    digest=wire.body_digest(body),
                    previous_digest=item.digest,
                    manifest=schema.canonical_bytes(signed),
                )
            assert not opened_slots
            resume.set()
            response = await asyncio.wait_for(callback, 5)
            assert response.status_code == 409
            assert response.json() == {"code": "stale_generation"}
            assert ticket.scope is None
            assert not opened_slots
            with pytest.raises(VaultProtocolError, match="^stale_generation$"):
                await ticket.wait()
        finally:
            resume.set()
            await asyncio.gather(callback, return_exceptions=True)


async def test_lock_closes_transport_before_join_and_disposes_afterwards(paired):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        entered, transport_closed = asyncio.Event(), asyncio.Event()
        cleanup_order = []

        async def task():
            async with service.work(operation="turn", use_case="turn", refs=[REF], ttl=60) as work:
                work.resources.callback(transport_closed.set)
                work.finalizers.callback(cleanup_order.append, "disposed")

                async def child():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await transport_closed.wait()
                        cleanup_order.append("joined")

                work.start(child)

        owner = asyncio.create_task(task())
        try:
            grant = phone.issue(await pending(client), now=int(time.time())).envelope
            assert (await client.post(GRANT, json=grant)).status_code == 204
            await asyncio.wait_for(entered.wait(), 5)
            item = service.installation
            assert await asyncio.wait_for(
                runtime.apply_verified_control(
                    vault_id=item.vault_id,
                    epoch=item.epoch,
                    generation=item.generation,
                    state="locked",
                ),
                5,
            )
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert cleanup_order == ["joined", "disposed"]
        finally:
            transport_closed.set()
            if not owner.done():
                owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)


async def test_signerless_runtime_does_not_claim_leadership(paired):
    store, signer, phone = paired
    row = await store.load(phone.vault_id)
    async with VaultRuntime(store, None) as signerless:
        assert signerless._worker is None
        async with VaultRuntime(store, signer) as configured:
            service = await until(configured.serving)
            assert service.installation.generation == row["generation"] + 1


async def test_lock_joins_finalizer_already_running_when_owner_is_cancelled(paired):
    store, signer, phone = paired
    async with VaultRuntime(store, signer) as runtime, http(runtime) as client:
        service = await until(runtime.serving)
        started, release, finished, interrupted = (asyncio.Event() for _ in range(4))

        async def finalizer():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            finished.set()

        async def task():
            async with service.work(operation="turn", use_case="turn", refs=[REF], ttl=60) as work:
                work.finalizers.push_async_callback(finalizer)

        owner = asyncio.create_task(task())
        control = None
        try:
            grant = phone.issue(await pending(client), now=int(time.time())).envelope
            assert (await client.post(GRANT, json=grant)).status_code == 204
            await asyncio.wait_for(started.wait(), 5)
            item = service.installation
            control = asyncio.create_task(
                runtime.apply_verified_control(
                    vault_id=item.vault_id,
                    epoch=item.epoch,
                    generation=item.generation,
                    state="locked",
                )
            )
            async with asyncio.timeout(5):
                while (await store.load(item.vault_id))["state"] != "locked":
                    await asyncio.sleep(0.01)
            assert owner.cancelling() > 0
            assert not control.done()
            assert not interrupted.is_set()
            owner.cancel()
            release.set()
            assert await asyncio.wait_for(control, 5)
            assert finished.is_set()
            assert not interrupted.is_set()
            with pytest.raises(asyncio.CancelledError):
                await owner
        finally:
            release.set()
            if not owner.done():
                owner.cancel()
            await asyncio.gather(
                *(task for task in (owner, control) if task is not None), return_exceptions=True
            )


@pytest.mark.parametrize("lose_connection", [False, True])
async def test_provider_pollers_follow_leadership_while_locked(
    db, paired, monkeypatch, lose_connection
):
    from aether import main
    from aether.vault import runtime as runtime_module

    _, _, phone = paired
    await db.execute("UPDATE vault_state SET state = 'locked' WHERE vault_id = $1", phone.vault_id)
    monkeypatch.setattr(runtime_module, "_CHECK_INTERVAL", 0.05)
    calls = []
    active = set()
    identity_keys = []
    started = asyncio.Event()
    settled = asyncio.Event()
    promoted = asyncio.Event()

    async def transport(ingress, token, owners, identity_key):
        assert not active
        assert owners == frozenset({42})
        identity_keys.append(identity_key)
        active.add(token)
        calls.append(("start", token))
        (started if token == "first" else promoted).set()
        try:
            await asyncio.Future()
        finally:
            active.remove(token)
            calls.append(("stop", token))
            if token == "first":
                settled.set()

    def app_for(token):
        return main.create_app(
            settings=Settings.without_env_file(
                database_url=os.environ["AETHER_TEST_DATABASE_URL"],
                vault_request_signing_key=schema.b64u_encode(SERVER_PRIVATE_RAW),
                vault_ingress_identity_key=schema.b64u_encode(b"i" * 32),
                telegram_bot_token=token,
                telegram_ingress_owners=frozenset({42}),
            )
        )

    monkeypatch.setattr(main, "watch_telegram", transport)
    first, second = app_for("first"), app_for("second")
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as first_life:
        await first_life.enter_async_context(first.router.lifespan_context(first))
        await asyncio.wait_for(started.wait(), 3)
        async with second.router.lifespan_context(second):
            await asyncio.sleep(0.15)
            assert calls == [("start", "first")]
            if lose_connection:
                leader = first.state.vault_runtime._leader
                pid = leader._connection.get_server_pid()
                assert await db.fetchval("SELECT pg_terminate_backend($1)", pid)
                await asyncio.wait_for(settled.wait(), 3)
                await first_life.aclose()
            else:
                await first_life.aclose()
            await asyncio.wait_for(promoted.wait(), 3)
            assert calls == [("start", "first"), ("stop", "first"), ("start", "second")]
    assert not active
    assert len(identity_keys) == 2
    assert identity_keys[0] == identity_keys[1]
    assert len(identity_keys[0]) == 32
