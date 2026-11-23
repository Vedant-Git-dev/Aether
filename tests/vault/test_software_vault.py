from __future__ import annotations

import copy

import pytest
import reference_wire as wire
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from software_vault import SoftwareVault, VaultDenied, open_grant

from aether.vault import schema
from aether.vault.errors import VaultProtocolError

NOW = 1_700_000_000
VAULT_ID = bytes(range(16))
DEVICE_ID = bytes(range(16, 32))
SERVER_PRIVATE_RAW = bytes(range(32, 64))
SERVER_PUBLIC = (
    ec.derive_private_key(int.from_bytes(SERVER_PRIVATE_RAW, "big"), ec.SECP256R1())
    .public_key()
    .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
)
SERVER_KID = wire.signer_id(SERVER_PUBLIC)


def new_vault() -> SoftwareVault:
    vault = SoftwareVault(
        vault_id=VAULT_ID,
        device_id=DEVICE_ID,
        origin="https://vault.example.test",
        server_public=SERVER_PUBLIC,
        server_kid=SERVER_KID,
        phone_private=bytes(range(1, 33)),
        hardware_master=bytes(range(32, 64)),
        ingress_private=bytes(range(64, 96)),
        recovery_entropy=bytes(range(32)),
    )
    vault.enroll()
    vault.enable()
    return vault


def make_request(
    vault: SoftwareVault,
    *,
    epk: bytes,
    now: int = NOW,
    refs: list[dict] | None = None,
    operation: str = "turn",
    use_case: str = "turn",
    ttl: int = 60,
    request_id: bytes | None = None,
    nonce: bytes | None = None,
    boot: bytes | None = None,
    task: bytes | None = None,
    iat: int | None = None,
    accept_until: int | None = None,
    manifest_digest: bytes | None = None,
    epoch: int | None = None,
) -> tuple[dict, dict]:
    iat = now if iat is None else iat
    body = {
        "v": 1,
        "type": "lease_request",
        "vault": schema.b64u_encode(VAULT_ID),
        "device": schema.b64u_encode(DEVICE_ID),
        "epoch": vault.epoch if epoch is None else epoch,
        "boot": schema.b64u_encode(boot or bytes(range(96, 112))),
        "task": schema.b64u_encode(task or bytes(range(112, 128))),
        "operation": operation,
        "use_case": use_case,
        "request": schema.b64u_encode(request_id or bytes(range(128, 144))),
        "nonce": schema.b64u_encode(nonce or bytes(range(144, 176))),
        "iat": iat,
        "accept_until": iat + 30 if accept_until is None else accept_until,
        "refs": refs or [{"cat": "memory", "ver": 1}],
        "ttl": ttl,
        "epk": schema.b64u_encode(epk),
        "manifest_digest": schema.b64u_encode(
            manifest_digest
            if manifest_digest is not None
            else wire.body_digest(vault.manifest_body)
        ),
    }
    schema.validate_body(body)
    envelope = wire.sign_body(
        body, wire.ec_private_key(SERVER_PRIVATE_RAW), alg="ES256", kid=SERVER_KID
    )
    return envelope, body


def boot_pair() -> tuple[X25519PrivateKey, bytes]:
    private = X25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw()


def test_enroll_manifest_is_signed_and_complete() -> None:
    vault = new_vault()
    body = wire.verify_body(vault.manifest, wire.ec_public_key(vault.phone_public))
    assert body == vault.manifest_body
    schema.validate_body(body, expected_type="manifest")
    assert body["revision"] == 1
    assert body["previous_digest"] is None
    assert len(body["entries"]) == len(schema.CATEGORIES)
    assert [entry["cat"] for entry in body["entries"]] == list(schema.CATEGORIES)
    for entry in body["entries"]:
        assert entry["status"] == "active"
        assert (
            wire.device_unwrap(
                vault.hardware_master, VAULT_ID, entry["cat"], entry["ver"], entry["wrap"]
            )
            == vault.category_keys[entry["cat"], entry["ver"]]
        )
        assert (
            wire.backup_unwrap(
                vault.recovery.hpke_private, VAULT_ID, entry["cat"], entry["ver"], entry["backup"]
            )
            == vault.category_keys[entry["cat"], entry["ver"]]
        )
    assert len(vault.phrase.split()) == 24


def test_issue_grants_exactly_the_requested_refs() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, body = make_request(
        vault,
        epk=epk,
        refs=[{"cat": "memory", "ver": 1}, {"cat": "operations", "ver": 1}],
    )
    issued = vault.issue(request, now=NOW)
    grant_body, keys = open_grant(
        issued.envelope,
        body,
        phone_public=vault.phone_public,
        boot_private=boot.private_bytes_raw(),
        now=NOW,
    )
    assert grant_body["refs"] == body["refs"]
    assert grant_body["exp"] == NOW + body["ttl"]
    assert grant_body["epoch"] == vault.epoch
    assert keys == {
        ("memory", 1): vault.category_keys["memory", 1],
        ("operations", 1): vault.category_keys["operations", 1],
    }


def test_issue_denies_while_locked() -> None:
    vault = new_vault()
    vault.lock()
    boot, epk = boot_pair()
    request, body = make_request(vault, epk=epk)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "locked"
    denial = wire.verify_body(caught.value.envelope, wire.ec_public_key(vault.phone_public))
    schema.validate_body(denial, expected_type="lease_denied")
    assert denial["request_digest"] == schema.b64u_encode(wire.body_digest(body))


def test_issue_denies_a_stale_epoch() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    old_epoch = vault.epoch
    vault.enable()
    request, _ = make_request(vault, epk=epk, epoch=old_epoch)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "stale"


def test_issue_denies_a_wrong_manifest_digest() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, _ = make_request(vault, epk=epk, manifest_digest=b"\x00" * 32)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "manifest"


def test_issue_denies_an_expired_accept_window() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, _ = make_request(vault, epk=epk, iat=NOW - 31, accept_until=NOW - 1)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "stale"


def test_issue_denies_a_future_timestamp_beyond_skew() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, _ = make_request(vault, epk=epk, iat=NOW + 16, accept_until=NOW + 46)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "stale"


def test_issue_denies_a_replayed_request() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, _ = make_request(vault, epk=epk)
    vault.issue(request, now=NOW)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW + 1)
    assert caught.value.reason == "stale"


def test_issue_denies_a_reused_nonce_under_a_new_request() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    nonce = bytes(range(144, 176))
    first, _ = make_request(vault, epk=epk, request_id=bytes(range(128, 144)), nonce=nonce)
    vault.issue(first, now=NOW)
    second, _ = make_request(vault, epk=epk, request_id=bytes(range(160, 176)), nonce=nonce)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(second, now=NOW + 1)
    assert caught.value.reason == "stale"


def test_issue_denies_when_the_burst_is_spent() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    for index in range(6):
        request, _ = make_request(
            vault,
            epk=epk,
            request_id=index.to_bytes(16, "big"),
            nonce=index.to_bytes(32, "big"),
        )
        vault.issue(request, now=NOW)
    seventh, _ = make_request(
        vault, epk=epk, request_id=(99).to_bytes(16, "big"), nonce=(99).to_bytes(32, "big")
    )
    with pytest.raises(VaultDenied) as caught:
        vault.issue(seventh, now=NOW)
    assert caught.value.reason == "rate"


def test_issue_rejects_a_bad_server_signature_without_an_oracle() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, _ = make_request(vault, epk=epk)
    request["body"]["ttl"] = 61
    with pytest.raises(VaultProtocolError) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.code == "bad_signature"


def test_deny_is_signed_and_closed() -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    _, body = make_request(vault, epk=epk)
    envelope = vault.deny(body, "user_required", now=NOW)
    denial = wire.verify_body(envelope, wire.ec_public_key(vault.phone_public))
    schema.validate_body(denial, expected_type="lease_denied")
    assert denial["reason"] == "user_required"


def test_restore_recovers_the_same_keys_under_a_new_master() -> None:
    vault = new_vault()
    old_keys = dict(vault.category_keys)
    old_master = vault.hardware_master
    old_phone = vault.phone_public
    old_epoch = vault.epoch
    backup = copy.deepcopy(vault.manifest)
    new_device = bytes(range(48, 64))
    new_phone_private = bytes(range(80, 112))
    new_master = bytes(range(112, 144))

    manifest = vault.restore(
        vault.phrase,
        backup,
        new_device_id=new_device,
        new_phone_private=new_phone_private,
        new_hardware_master=new_master,
        now=NOW,
    )
    body = wire.verify_body(manifest, wire.ec_public_key(vault.phone_public))
    schema.validate_body(body, expected_type="manifest")
    assert vault.phone_public != old_phone
    assert vault.epoch == old_epoch + 1
    assert body["revision"] == 2
    assert body["previous_digest"] == schema.b64u_encode(wire.body_digest(backup["body"]))
    assert vault.state == "locked"
    assert vault.category_keys == old_keys
    for entry in body["entries"]:
        assert (
            wire.device_unwrap(new_master, VAULT_ID, entry["cat"], entry["ver"], entry["wrap"])
            == old_keys[entry["cat"], entry["ver"]]
        )
        assert (
            wire.backup_unwrap(
                vault.recovery.hpke_private, VAULT_ID, entry["cat"], entry["ver"], entry["backup"]
            )
            == old_keys[entry["cat"], entry["ver"]]
        )
    with pytest.raises(VaultProtocolError):
        wire.device_unwrap(new_master, VAULT_ID, "memory", 1, backup["body"]["entries"][3]["wrap"])
    assert old_master != new_master


def test_restore_rejects_a_wrong_phrase() -> None:
    vault = new_vault()
    backup = copy.deepcopy(vault.manifest)
    wrong_phrase = wire.phrase_from_entropy(bytes(range(1, 33)))
    with pytest.raises(VaultProtocolError) as caught:
        vault.restore(wrong_phrase, backup, new_device_id=bytes(16), now=NOW)
    assert caught.value.code == "bad_signature"


def test_restore_rejects_a_tampered_backup() -> None:
    vault = new_vault()
    backup = copy.deepcopy(vault.manifest)
    backup["body"]["entries"][0]["backup"]["ct"] = schema.b64u_encode(b"\xff" * 16)
    backup = wire.sign_body(
        backup["body"], wire.ec_private_key(vault.phone_private), alg="ES256", kid=vault.phone_kid
    )
    with pytest.raises(VaultProtocolError) as caught:
        vault.restore(vault.phrase, backup, new_device_id=bytes(16), now=NOW)
    assert caught.value.code == "denied"


def test_read_only_refs_require_a_read_operation() -> None:
    vault = new_vault()
    body = copy.deepcopy(vault.manifest_body)
    memory_v1 = next(entry for entry in body["entries"] if entry["cat"] == "memory")
    memory_v1["status"] = "read_only"
    memory_v2_key = b"\xaa" * 32
    body["entries"].append(
        {
            "cat": "memory",
            "ver": 2,
            "status": "active",
            "wrap": wire.device_wrap(vault.hardware_master, VAULT_ID, "memory", 2, memory_v2_key),
            "backup": wire.backup_wrap(
                vault.recovery.hpke_public, VAULT_ID, "memory", 2, memory_v2_key
            ),
        }
    )
    body["entries"].sort(key=lambda entry: (entry["cat"], entry["ver"]))
    schema.validate_body(body)
    vault.manifest_body = body
    vault.manifest = wire.sign_body(
        body, wire.ec_private_key(vault.phone_private), alg="ES256", kid=vault.phone_kid
    )

    boot, epk = boot_pair()
    turn, _ = make_request(vault, epk=epk, refs=[{"cat": "memory", "ver": 1}])
    with pytest.raises(VaultDenied) as caught:
        vault.issue(turn, now=NOW)
    assert caught.value.reason == "bad_scope"

    drain, drain_body = make_request(
        vault,
        epk=epk,
        operation="drain",
        use_case="drain",
        refs=[{"cat": "memory", "ver": 1}],
        request_id=bytes(range(160, 176)),
        nonce=bytes(range(176, 208)),
    )
    issued = vault.issue(drain, now=NOW + 1)
    _, keys = open_grant(
        issued.envelope,
        drain_body,
        phone_public=vault.phone_public,
        boot_private=boot.private_bytes_raw(),
        now=NOW,
    )
    assert keys == {("memory", 1): vault.category_keys["memory", 1]}


@pytest.mark.parametrize("field", ["vault", "device", "kid"])
def test_issue_rejects_wrong_pairing_pin(field: str) -> None:
    vault = new_vault()
    _, epk = boot_pair()
    _, body = make_request(vault, epk=epk)
    kid = SERVER_KID
    if field == "kid":
        kid = schema.b64u_encode(bytes(32))
    else:
        body[field] = schema.b64u_encode(bytes(16))
    request = wire.sign_body(body, wire.ec_private_key(SERVER_PRIVATE_RAW), alg="ES256", kid=kid)
    with pytest.raises(VaultProtocolError):
        vault.issue(request, now=NOW)


@pytest.mark.parametrize("operation", ["maintenance", "credential_write"])
def test_sensitive_grant_requires_local_consent(operation: str) -> None:
    vault = new_vault()
    _, epk = boot_pair()
    request, _ = make_request(
        vault,
        epk=epk,
        operation=operation,
        use_case=operation,
        refs=[{"cat": "credentials", "ver": 1}],
    )
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "user_required"
    vault.consent(request)
    assert vault.issue(request, now=NOW).body["refs"] == [{"cat": "credentials", "ver": 1}]
    with pytest.raises(VaultDenied) as replay:
        vault.issue(request, now=NOW)
    assert replay.value.reason == "stale"


def test_issue_denies_elapsed_ttl_inside_accept_window() -> None:
    vault = new_vault()
    _, epk = boot_pair()
    request, _ = make_request(vault, epk=epk, iat=NOW - 2, ttl=1)
    with pytest.raises(VaultDenied) as caught:
        vault.issue(request, now=NOW)
    assert caught.value.reason == "stale"


def test_restore_keeps_distinct_key_versions() -> None:
    vault = new_vault()
    old_key = vault.category_keys["memory", 1]
    new_key = bytes([235]) * 32
    body = copy.deepcopy(vault.manifest_body)
    body["entries"].append(vault._entry("memory", 2, "active", new_key))
    for entry in body["entries"]:
        if (entry["cat"], entry["ver"]) == ("memory", 1):
            entry["status"] = "read_only"
    body["entries"].sort(key=lambda entry: (entry["cat"], entry["ver"]))
    body["revision"] = 2
    body["previous_digest"] = schema.b64u_encode(wire.body_digest(vault.manifest_body))
    backup = wire.sign_body(
        body, wire.ec_private_key(vault.phone_private), alg="ES256", kid=vault.phone_kid
    )
    restored = vault.restore(vault.phrase, backup, new_device_id=bytes(16), now=NOW)
    for entry in restored["body"]["entries"]:
        if entry["cat"] == "memory":
            assert (
                wire.device_unwrap(
                    vault.hardware_master, VAULT_ID, "memory", entry["ver"], entry["wrap"]
                )
                == {1: old_key, 2: new_key}[entry["ver"]]
            )


@pytest.mark.parametrize(
    "field",
    [
        "vault",
        "device",
        "epoch",
        "boot",
        "task",
        "request",
        "nonce",
        "manifest_digest",
        "request_digest",
        "refs",
        "exp",
    ],
)
def test_open_grant_rejects_each_changed_binding(field: str) -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, body = make_request(vault, epk=epk)
    grant = vault.issue(request, now=NOW).body
    if field in ("epoch", "exp"):
        grant[field] += 1
    elif field == "refs":
        grant[field] = [{"cat": "operations", "ver": 1}]
    else:
        raw = schema.b64u_decode(grant[field], field=field)
        grant[field] = schema.b64u_encode(bytes([raw[0] ^ 1]) + raw[1:])
    signed = wire.sign_body(
        grant, wire.ec_private_key(vault.phone_private), alg="ES256", kid=vault.phone_kid
    )
    with pytest.raises(VaultProtocolError) as caught:
        open_grant(
            signed,
            body,
            phone_public=vault.phone_public,
            boot_private=boot.private_bytes_raw(),
            now=NOW,
        )
    assert str(caught.value) == "grant does not match a current request"


@pytest.mark.parametrize("elapsed, ttl", [(31, 60), (2, 1)])
def test_open_grant_rejects_expired_or_late_delivery(elapsed: int, ttl: int) -> None:
    vault = new_vault()
    boot, epk = boot_pair()
    request, body = make_request(vault, epk=epk, ttl=ttl)
    grant = vault.issue(request, now=NOW)
    with pytest.raises(VaultProtocolError, match="grant does not match a current request"):
        open_grant(
            grant.envelope,
            body,
            phone_public=vault.phone_public,
            boot_private=boot.private_bytes_raw(),
            now=NOW + elapsed,
        )
