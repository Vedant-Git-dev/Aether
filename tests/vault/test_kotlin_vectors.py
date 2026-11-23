from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import reference_wire as wire

from aether.vault import schema
from aether.vault.errors import VaultProtocolError

KOTLIN = Path(__file__).with_name("vectors") / "kotlin"


def load(name: str) -> dict:
    return json.loads((KOTLIN / name).read_text(encoding="utf-8"))


def test_kotlin_hpke_vector_opens_and_reproduces_bytes() -> None:
    fixture = load("hpke_kotlin.json")
    assert fixture["aad"] == schema.b64u_encode(schema.canonical_bytes(fixture["header"]))
    assert fixture["info"] == "aether/v1/" + fixture["usage"]
    plaintext = schema.b64u_decode(fixture["plaintext"], field="plaintext")
    assert (
        wire.hpke_open(
            schema.b64u_decode(fixture["recipient_private"], field="recipient_private"),
            fixture["usage"],
            fixture["header"],
            fixture["box"],
        )
        == plaintext
    )
    resealed = wire.hpke_seal(
        schema.b64u_decode(fixture["recipient_public"], field="recipient_public"),
        fixture["usage"],
        fixture["header"],
        plaintext,
        ephemeral_private=schema.b64u_decode(
            fixture["ephemeral_private"], field="ephemeral_private"
        ),
    )
    assert resealed == fixture["box"]


def test_kotlin_hpke_vector_rejects_a_changed_aad() -> None:
    fixture = load("hpke_kotlin.json")
    header = copy.deepcopy(fixture["header"])
    header["lease"] = schema.b64u_encode(b"\xff" * 16)
    with pytest.raises(VaultProtocolError) as caught:
        wire.hpke_open(
            schema.b64u_decode(fixture["recipient_private"], field="recipient_private"),
            fixture["usage"],
            header,
            fixture["box"],
        )
    assert caught.value.code == "denied"


def test_kotlin_ed25519_vector_verifies() -> None:
    fixture = load("ed25519_kotlin.json")
    body = wire.verify_body(
        fixture["envelope"],
        wire.ed25519_public_key(schema.b64u_decode(fixture["public"], field="public")),
        algorithms=("Ed25519",),
    )
    schema.validate_body(body, expected_type="restore")
    assert fixture["body_b64"] == schema.b64u_encode(schema.canonical_bytes(body))
    assert fixture["signing_input"] == (
        fixture["envelope"]["protected"] + "." + fixture["body_b64"]
    )
    assert body == fixture["body"]


def test_kotlin_ed25519_vector_rejects_a_changed_body() -> None:
    fixture = load("ed25519_kotlin.json")
    envelope = copy.deepcopy(fixture["envelope"])
    envelope["body"]["iat"] += 1
    with pytest.raises(VaultProtocolError) as caught:
        wire.verify_body(
            envelope,
            wire.ed25519_public_key(schema.b64u_decode(fixture["public"], field="public")),
            algorithms=("Ed25519",),
        )
    assert caught.value.code == "bad_signature"


def test_kotlin_es256_vector_verifies() -> None:
    fixture = load("es256_kotlin.json")
    body = wire.verify_body(
        fixture["envelope"],
        wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public")),
    )
    schema.validate_body(body, expected_type="pair")
    assert fixture["body_b64"] == schema.b64u_encode(schema.canonical_bytes(body))
    assert fixture["signing_input"] == (
        fixture["envelope"]["protected"] + "." + fixture["body_b64"]
    )


def test_kotlin_es256_vector_rejects_a_changed_body() -> None:
    fixture = load("es256_kotlin.json")
    envelope = copy.deepcopy(fixture["envelope"])
    envelope["body"]["nonce"] = schema.b64u_encode(b"\xff" * 32)
    with pytest.raises(VaultProtocolError) as caught:
        wire.verify_body(
            envelope,
            wire.ec_public_key(schema.b64u_decode(fixture["public"], field="public")),
        )
    assert caught.value.code == "bad_signature"
