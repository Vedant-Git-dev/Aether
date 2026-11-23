from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import reference_wire as wire

from aether.vault import schema
from aether.vault.errors import ERROR_STATUS, VaultProtocolError

VECTORS = Path(__file__).with_name("vectors")


def load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def test_jcs_vectors_are_canonical() -> None:
    for case in load("jcs.json")["cases"]:
        assert schema.canonical_bytes(case["value"]) == case["canonical"].encode()


def test_es256_python_vector_verifies() -> None:
    fixture = load("es256_python.json")
    body = fixture["body"]
    envelope = fixture["envelope"]
    schema.validate_body(body, expected_type="pair")
    assert (
        wire.verify_body(
            envelope, wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public"))
        )
        == body
    )
    assert wire.body_digest(body) == schema.b64u_decode(fixture["digest"], field="digest")
    payload = schema.b64u_encode(schema.canonical_bytes(body))
    assert fixture["signing_input"] == f"{envelope['protected']}.{payload}"
    assert wire.signer_id(schema.b64u_decode(fixture["public"], field="public")) == fixture["kid"]


def test_es256_python_vector_rejects_a_changed_body() -> None:
    fixture = load("es256_python.json")
    envelope = copy.deepcopy(fixture["envelope"])
    envelope["body"]["iat"] += 1
    with pytest.raises(VaultProtocolError) as caught:
        wire.verify_body(
            envelope, wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public"))
        )
    assert caught.value.code == "bad_signature"


def test_ed25519_python_vector_verifies_and_is_deterministic() -> None:
    fixture = load("ed25519_python.json")
    public = schema.b64u_decode(fixture["public"], field="public")
    body = fixture["body"]
    envelope = fixture["envelope"]
    schema.validate_body(body, expected_type="restore")
    assert (
        wire.verify_body(envelope, wire.ed25519_public_key(public), algorithms=("Ed25519",)) == body
    )
    resigned = wire.sign_body(
        body,
        wire.ed25519_private_key(schema.b64u_decode(fixture["seed"], field="seed")),
        alg="Ed25519",
        kid=fixture["kid"],
    )
    assert resigned["signature"] == envelope["signature"]


def test_ed25519_python_vector_rejects_a_changed_field() -> None:
    fixture = load("ed25519_python.json")
    envelope = copy.deepcopy(fixture["envelope"])
    envelope["body"]["new_manifest_digest"] = schema.b64u_encode(b"\xff" * 32)
    with pytest.raises(VaultProtocolError) as caught:
        wire.verify_body(
            envelope,
            wire.ed25519_public_key(schema.b64u_decode(fixture["public"], field="public")),
            algorithms=("Ed25519",),
        )
    assert caught.value.code == "bad_signature"


def test_hpke_python_vector_opens_and_reproduces_bytes() -> None:
    fixture = load("hpke_python.json")
    recipient_private = schema.b64u_decode(fixture["recipient_private"], field="recipient_private")
    recipient_public = schema.b64u_decode(fixture["recipient_public"], field="recipient_public")
    header = fixture["header"]
    plaintext = schema.b64u_decode(fixture["plaintext"], field="plaintext")
    box = fixture["box"]
    assert wire.hpke_open(recipient_private, "grant", header, box) == plaintext
    resealed = wire.hpke_seal(
        recipient_public,
        "grant",
        header,
        plaintext,
        ephemeral_private=schema.b64u_decode(
            fixture["ephemeral_private"], field="ephemeral_private"
        ),
    )
    assert resealed == box


def test_hpke_rejects_a_changed_aad() -> None:
    fixture = load("hpke_python.json")
    header = copy.deepcopy(fixture["header"])
    header["task"] = schema.b64u_encode(b"\xff" * 16)
    with pytest.raises(VaultProtocolError) as caught:
        wire.hpke_open(
            schema.b64u_decode(fixture["recipient_private"], field="recipient_private"),
            "grant",
            header,
            fixture["box"],
        )
    assert caught.value.code == "denied"


def test_hpke_rejects_the_wrong_recipient() -> None:
    fixture = load("hpke_python.json")
    with pytest.raises(VaultProtocolError) as caught:
        wire.hpke_open(b"\x00" * 32, "grant", fixture["header"], fixture["box"])
    assert caught.value.code == "denied"


def test_ae2_vector_opens_and_reproduces_exact_bytes() -> None:
    fixture = load("ae2.json")
    key = schema.b64u_decode(fixture["key"], field="key")
    vault_id = schema.b64u_decode(fixture["vault"], field="vault")
    envelope = schema.b64u_decode(fixture["envelope"], field="envelope")
    plaintext = schema.b64u_decode(fixture["plaintext"], field="plaintext")
    assert (
        wire.ae2_open(
            key,
            vault_id,
            fixture["cat"],
            fixture["ver"],
            fixture["table"],
            fixture["column"],
            fixture["row"],
            fixture["purpose"],
            envelope,
        )
        == plaintext
    )
    resealed = wire.ae2_seal(
        key,
        vault_id,
        fixture["cat"],
        fixture["ver"],
        fixture["table"],
        fixture["column"],
        fixture["row"],
        fixture["purpose"],
        plaintext,
        nonce=schema.b64u_decode(fixture["nonce"], field="nonce"),
    )
    assert resealed == envelope


@pytest.mark.parametrize(
    "field, value",
    [
        ("row", "43"),
        ("table", "other"),
        ("column", "other"),
        ("purpose", "other"),
    ],
)
def test_ae2_binds_every_location_field(field: str, value: str) -> None:
    fixture = load("ae2.json")
    args = {
        "key": schema.b64u_decode(fixture["key"], field="key"),
        "vault_id": schema.b64u_decode(fixture["vault"], field="vault"),
        "cat": fixture["cat"],
        "ver": fixture["ver"],
        "table": fixture["table"],
        "column": fixture["column"],
        "row": fixture["row"],
        "purpose": fixture["purpose"],
        "blob": schema.b64u_decode(fixture["envelope"], field="envelope"),
    }
    args[field] = value
    with pytest.raises(VaultProtocolError):
        wire.ae2_open(
            args["key"],
            args["vault_id"],
            args["cat"],
            args["ver"],
            args["table"],
            args["column"],
            args["row"],
            args["purpose"],
            args["blob"],
        )


def test_ae2_rejects_a_changed_category_and_trailing_bytes() -> None:
    fixture = load("ae2.json")
    key = schema.b64u_decode(fixture["key"], field="key")
    vault_id = schema.b64u_decode(fixture["vault"], field="vault")
    envelope = schema.b64u_decode(fixture["envelope"], field="envelope")
    with pytest.raises(VaultProtocolError):
        wire.ae2_open(
            key,
            vault_id,
            "workspace",
            fixture["ver"],
            fixture["table"],
            fixture["column"],
            fixture["row"],
            fixture["purpose"],
            envelope,
        )
    with pytest.raises(VaultProtocolError):
        wire.ae2_open(
            key,
            vault_id,
            fixture["cat"],
            fixture["ver"],
            fixture["table"],
            fixture["column"],
            fixture["row"],
            fixture["purpose"],
            envelope + b"x",
        )


def test_recovery_vector_derives_the_published_keys() -> None:
    fixture = load("recovery.json")
    entropy = schema.b64u_decode(fixture["entropy"], field="entropy")
    vault_id = schema.b64u_decode(fixture["vault"], field="vault")
    keys = wire.recovery_keys(entropy, vault_id)
    assert wire.entropy_from_phrase(wire.phrase_from_entropy(entropy)) == entropy
    assert keys.hpke_private == schema.b64u_decode(fixture["hpke_private"], field="hpke_private")
    assert keys.hpke_public == schema.b64u_decode(fixture["hpke_public"], field="hpke_public")
    assert keys.sign_seed == schema.b64u_decode(fixture["sign_seed"], field="sign_seed")
    assert keys.sign_public == schema.b64u_decode(fixture["sign_public"], field="sign_public")


def test_recovery_phrase_rejects_a_changed_word() -> None:
    fixture = load("recovery.json")
    words = wire.phrase_from_entropy(
        schema.b64u_decode(fixture["entropy"], field="entropy")
    ).split()
    words[0] = "abandon" if words[0] != "abandon" else "ability"
    with pytest.raises(VaultProtocolError):
        wire.entropy_from_phrase(" ".join(words))


def test_wrap_vectors_open_and_reject_tampering() -> None:
    fixture = load("wraps.json")
    master = schema.b64u_decode(fixture["master"], field="master")
    vault_id = schema.b64u_decode(fixture["vault"], field="vault")
    key = schema.b64u_decode(fixture["key"], field="key")
    assert (
        wire.device_unwrap(master, vault_id, fixture["cat"], fixture["ver"], fixture["device"])
        == key
    )
    assert (
        wire.backup_unwrap(
            schema.b64u_decode(fixture["recovery_private"], field="recovery_private"),
            vault_id,
            fixture["cat"],
            fixture["ver"],
            fixture["backup"],
        )
        == key
    )
    with pytest.raises(VaultProtocolError):
        wire.device_unwrap(master, vault_id, "workspace", fixture["ver"], fixture["device"])
    with pytest.raises(VaultProtocolError):
        wire.backup_unwrap(
            schema.b64u_decode(fixture["recovery_private"], field="recovery_private"),
            vault_id,
            "workspace",
            fixture["ver"],
            fixture["backup"],
        )
    with pytest.raises(VaultProtocolError):
        wire.device_unwrap(
            b"\x00" * 32, vault_id, fixture["cat"], fixture["ver"], fixture["device"]
        )


def test_denial_vector_verifies_and_is_closed() -> None:
    fixture = load("denial.json")
    body = wire.verify_body(
        fixture["envelope"],
        wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public")),
    )
    schema.validate_body(body, expected_type="lease_denied")
    assert body["reason"] in schema.DENIAL_REASONS


def test_error_vectors_match_the_catalog() -> None:
    for case in load("errors.json")["cases"]:
        assert ERROR_STATUS[case["code"]] == case["status"]
        assert schema.validate_error_payload({"code": case["code"]}) == {"code": case["code"]}


def test_envelope_rejects_duplicate_protected_keys() -> None:
    fixture = load("es256_python.json")
    envelope = copy.deepcopy(fixture["envelope"])
    protected = '{"alg":"ES256","alg":"ES256","kid":"' + fixture["kid"] + '"}'
    envelope["protected"] = schema.b64u_encode(protected.encode())
    with pytest.raises(VaultProtocolError) as caught:
        wire.verify_body(
            envelope, wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public"))
        )
    assert caught.value.code == "duplicate_field"


def test_rfc9180_appendix_a_1_base_mode() -> None:
    """Published X25519/HKDF-SHA256/AES-128-GCM vector, sequence zero."""
    from pyhpke import AEADId, CipherSuite, KDFId, KEMId

    suite = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES128_GCM)
    ephemeral = suite.kem.derive_key_pair(
        bytes.fromhex("7268600d403fce431561aef583ee1613527cff655c1343f29812e66706df3234")
    )
    recipient = suite.kem.derive_key_pair(
        bytes.fromhex("6db9df30aa07dd42ee5e8181afdb977e538f5e1fec8a06223f33f7013e525037")
    )
    info = bytes.fromhex("4f6465206f6e2061204772656369616e2055726e")
    aad = bytes.fromhex("436f756e742d30")
    plaintext = bytes.fromhex("4265617574792069732074727574682c20747275746820626561757479")
    enc, sender = suite.create_sender_context(recipient.public_key, info, eks=ephemeral)
    assert enc.hex() == "37fda3567bdbd628e88668c3c8d7e97d1d1253b6d4ea6d44c150f741f1bf4431"
    ciphertext = sender.seal(plaintext, aad)
    assert (
        ciphertext.hex()
        == "f938558b5d72f1a23810b4be2ab4f84331acc02fc97babc53a52ae8218a355a96d8770ac83d07bea87e13c512a"
    )
    assert (
        suite.create_recipient_context(enc, recipient.private_key, info).open(ciphertext, aad)
        == plaintext
    )
