from __future__ import annotations

import asyncio

import pytest
import reference_wire as wire
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aether.vault import native, native_grant, schema
from aether.vault.errors import VaultProtocolError
from aether.vault.scope import Location, VaultClosed, VaultExhausted, VaultLocked
from aether.vault.service import Installation, LeaseService
from aether.vault.signing import RequestSigner
from aether.vault.store import VaultStore

pytestmark = pytest.mark.integration

VAULT = b"v" * 16
PHONE = b"p" * 16
DIGEST = b"m" * 32
REF = ("memory", 1)


@pytest.fixture
async def running(db, monkeypatch):
    try:
        native._sodium()
        native_grant._openssl()
    except VaultLocked as exc:
        pytest.skip(str(exc))
    monkeypatch.setitem(schema.USE_CASES, "test.turn", ("turn", frozenset({"memory"})))
    server = RequestSigner(ec.generate_private_key(ec.SECP256R1()))
    phone = wire.ec_private_key(b"p" * 32)
    phone_public = (
        phone.get_op_key("sign")
        .public_key()
        .public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint,
        )
    )
    store = VaultStore(db)
    await store.create(
        vault_id=VAULT,
        device_id=PHONE,
        digest=DIGEST,
        manifest=b"manifest",
        pins=b"pins",
        state="available",
    )
    async with store.serving_boot() as boot:
        state = await store.load(VAULT)
        install = Installation(
            VAULT,
            PHONE,
            state["generation"],
            state["epoch"],
            DIGEST,
            server.kid,
            phone_public,
            {REF: "active"},
        )
        clock = [1000.0]
        service = LeaseService(
            store, boot, install, server, wall=lambda: clock[0], monotonic=lambda: clock[0]
        )
        try:
            yield service, store, clock, phone, phone_public
        finally:
            service.close()


async def request(service, task=b"t" * 16):
    return await service.begin(
        task=task,
        operation="turn",
        use_case="test.turn",
        refs=[REF],
        ttl=60,
        writes={REF: 1},
    )


def reply(ticket, phone, public, *, deny=False, updates=None):
    request_body = schema.loads_strict(ticket.wire)["body"]
    body = {
        name: request_body[name]
        for name in ("v", "vault", "device", "epoch", "boot", "task", "request", "nonce", "iat")
    }
    body["request_digest"] = schema.b64u_encode(wire.body_digest(request_body))
    if deny:
        body.update(type="lease_denied", reason="locked")
    else:
        body.update(
            type="lease_grant",
            lease=schema.b64u_encode(b"l" * 16),
            exp=body["iat"] + request_body["ttl"],
            refs=request_body["refs"],
            manifest_digest=request_body["manifest_digest"],
        )
    body.update(updates or {})
    if not deny:
        plaintext = schema.canonical_bytes(
            {"keys": [{"cat": "memory", "ver": 1, "key": schema.b64u_encode(b"k" * 32)}]}
        )
        body["box"] = wire.hpke_seal(
            schema.b64u_decode(request_body["epk"], field="epk"),
            "grant",
            body,
            plaintext,
        )
    return wire.sign_body(body, phone, alg="ES256", kid=wire.signer_id(public))


async def test_coalesces_outstanding_request_and_installs_exact_grant_once(running):
    service, _, _, phone, public = running
    ticket, same = await asyncio.gather(request(service), request(service))
    assert ticket is same
    assert await service.pending() == ticket.wire
    envelope = reply(ticket, phone, public)
    await asyncio.gather(*(service.callback(envelope, kind="lease_grant") for _ in range(2)))
    scope = await ticket.wait()
    assert scope.refs == (REF,)
    assert await service.pending() is None
    location = Location("memory", 1, "events", "payload_enc", "42")
    assert scope.open(location, scope.seal(location, b"sample")) == b"sample"
    with pytest.raises(VaultExhausted):
        scope.seal(location, b"another write")
    service.end(scope.task)
    assert scope.closed
    await service.callback(envelope, kind="lease_grant")
    with pytest.raises(VaultProtocolError, match="^replay$"):
        changed = reply(ticket, phone, public, updates={"lease": schema.b64u_encode(b"M" * 16)})
        await service.callback(changed, kind="lease_grant")


async def test_refuses_wrong_signer_boot_deadline_and_denial(running):
    service, _, clock, phone, public = running
    ticket = await request(service)
    impostor = wire.ec_private_key(b"i" * 32)
    with pytest.raises(VaultProtocolError, match="^bad_signature$"):
        await service.callback(reply(ticket, impostor, public), kind="lease_grant")
    wrong = reply(ticket, phone, public, updates={"boot": schema.b64u_encode(b"b" * 16)})
    with pytest.raises(VaultProtocolError, match="^stale_generation$"):
        await service.callback(wrong, kind="lease_grant")
    await service.callback(reply(ticket, phone, public, deny=True), kind="lease_denied")
    with pytest.raises(VaultProtocolError, match="^denied$"):
        await ticket.wait()
    next_ticket = await request(service)
    clock[0] += 31
    with pytest.raises(VaultProtocolError, match="^expired_request$"):
        await service.callback(reply(next_ticket, phone, public), kind="lease_grant")
    assert await service.pending() is None


async def test_cancel_during_acceptance_prevents_opening(running, monkeypatch):
    service, store, _, phone, public = running
    ticket = await request(service)
    reserve = store.reserve_aead
    started, proceed = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        started.set()
        await proceed.wait()
        return await reserve(**kwargs)

    monkeypatch.setattr(store, "reserve_aead", delayed)
    monkeypatch.setattr(
        service._recipient, "open_grant_slots", lambda _: pytest.fail("opened a cancelled grant")
    )
    callback = asyncio.create_task(
        service.callback(reply(ticket, phone, public), kind="lease_grant")
    )
    await asyncio.wait_for(started.wait(), 5)
    service.end(b"t" * 16)
    proceed.set()
    with pytest.raises(VaultProtocolError, match="^conflict$"):
        await callback
    with pytest.raises(VaultProtocolError, match="^conflict$"):
        await ticket.wait()


async def test_expired_scope_denies_key_use_before_timer_runs(running):
    service, _, clock, phone, public = running
    ticket = await request(service)
    await service.callback(reply(ticket, phone, public), kind="lease_grant")
    scope = await ticket.wait()
    clock[0] += 60
    with pytest.raises(VaultClosed, match="expired"):
        scope.require(*REF)
    assert scope.closed


async def test_closing_one_scope_leaves_another_usable_until_clock_discontinuity(running):
    service, _, clock, phone, public = running
    first, second = await asyncio.gather(request(service), request(service, b"o" * 16))
    for ticket in (first, second):
        await service.callback(reply(ticket, phone, public), kind="lease_grant")
    one, two = await first.wait(), await second.wait()
    service.end(one.task)
    assert one.closed
    two.require(*REF)
    service._wall = lambda: clock[0] - 10
    with pytest.raises(VaultClosed, match="clock continuity"):
        two.require(*REF)
    assert two.closed
