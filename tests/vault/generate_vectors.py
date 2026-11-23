"""Regenerate the public Python-side wire vectors.

Run from the repository root: ``python tests/vault/generate_vectors.py``.
The fixtures contain fixed test keys and synthetic payloads only. They are
committed so both languages verify the same bytes; the tests do not write them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reference_wire as wire  # noqa: E402

from aether.vault import schema  # noqa: E402
from aether.vault.errors import ERROR_STATUS  # noqa: E402

VECTORS = Path(__file__).resolve().parent / "vectors"


def write(name: str, value: object) -> None:
    path = VECTORS / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def p256_public(raw: bytes) -> bytes:
    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def jcs() -> None:
    cases = [
        {"value": {"b": 2, "a": 1}, "canonical": '{"a":1,"b":2}'},
        {"value": [1, 2, 3], "canonical": "[1,2,3]"},
        {
            "value": {"x": True, "y": None, "z": "text"},
            "canonical": '{"x":true,"y":null,"z":"text"}',
        },
        {"value": {"n": 0}, "canonical": '{"n":0}'},
        {"value": {"esc": "a\nb\tc"}, "canonical": '{"esc":"a\\nb\\tc"}'},
        {"value": {"unicode": "caf\u00e9"}, "canonical": '{"unicode":"caf\u00e9"}'},
    ]
    write("jcs.json", {"cases": cases})


def es256() -> None:
    private_raw = bytes(range(1, 33))
    public_raw = p256_public(private_raw)
    body = {
        "v": 1,
        "type": "pair",
        "vault": schema.b64u_encode(bytes(range(16))),
        "device": schema.b64u_encode(bytes(range(16, 32))),
        "epoch": 1,
        "origin": "https://vault.example.test",
        "server_kid": schema.b64u_encode(bytes(range(32))),
        "phone_pub": schema.b64u_encode(public_raw),
        "manifest_digest": schema.b64u_encode(bytes(range(32, 64))),
        "nonce": schema.b64u_encode(bytes(range(64, 96))),
        "iat": 1_700_000_000,
    }
    kid = wire.signer_id(public_raw)
    envelope = wire.sign_body(body, wire.ec_private_key(private_raw), alg="ES256", kid=kid)
    payload = schema.b64u_encode(schema.canonical_bytes(body))
    write(
        "es256_python.json",
        {
            "private": schema.b64u_encode(private_raw),
            "public": schema.b64u_encode(public_raw),
            "kid": kid,
            "body": body,
            "envelope": envelope,
            "body_b64": payload,
            "signing_input": f"{envelope['protected']}.{payload}",
            "digest": schema.b64u_encode(wire.body_digest(body)),
        },
    )


def ed25519() -> None:
    seed = bytes(range(32, 64))
    private = wire.ed25519_private_key(seed)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    body = {
        "v": 1,
        "type": "restore",
        "vault": schema.b64u_encode(bytes(range(16))),
        "device": schema.b64u_encode(bytes(range(16, 32))),
        "epoch": 7,
        "challenge": schema.b64u_encode(bytes(range(96, 128))),
        "previous_digest": schema.b64u_encode(bytes(range(32, 64))),
        "new_manifest_digest": schema.b64u_encode(bytes(range(64, 96))),
        "new_phone_pub": schema.b64u_encode(p256_public(bytes(range(1, 33)))),
        "iat": 1_700_000_100,
    }
    kid = wire.signer_id(public)
    envelope = wire.sign_body(body, private, alg="Ed25519", kid=kid)
    payload = schema.b64u_encode(schema.canonical_bytes(body))
    write(
        "ed25519_python.json",
        {
            "seed": schema.b64u_encode(seed),
            "public": schema.b64u_encode(public),
            "kid": kid,
            "body": body,
            "envelope": envelope,
            "body_b64": payload,
            "signing_input": f"{envelope['protected']}.{payload}",
            "digest": schema.b64u_encode(wire.body_digest(body)),
        },
    )


def hpke() -> None:
    recipient_private = bytes(range(16, 48))
    ephemeral_private = bytes(range(64, 96))
    recipient_public = (
        X25519PrivateKey.from_private_bytes(recipient_private).public_key().public_bytes_raw()
    )
    ephemeral_public = (
        X25519PrivateKey.from_private_bytes(ephemeral_private).public_key().public_bytes_raw()
    )
    header = {
        "v": 1,
        "type": "lease_grant",
        "vault": schema.b64u_encode(bytes(range(16))),
        "device": schema.b64u_encode(bytes(range(16, 32))),
        "epoch": 3,
        "boot": schema.b64u_encode(bytes(range(32, 48))),
        "task": schema.b64u_encode(bytes(range(48, 64))),
        "request": schema.b64u_encode(bytes(range(64, 80))),
        "nonce": schema.b64u_encode(bytes(range(80, 112))),
        "request_digest": schema.b64u_encode(bytes(range(112, 144))),
        "lease": schema.b64u_encode(bytes(range(144, 160))),
        "iat": 1_700_000_200,
        "exp": 1_700_000_260,
        "refs": [{"cat": "memory", "ver": 1}, {"cat": "operations", "ver": 1}],
        "manifest_digest": schema.b64u_encode(bytes(range(160, 192))),
    }
    plaintext = schema.canonical_bytes(
        {
            "keys": [
                {"cat": "memory", "ver": 1, "key": schema.b64u_encode(bytes(range(192, 224)))},
                {"cat": "operations", "ver": 1, "key": schema.b64u_encode(bytes(range(224, 256)))},
            ]
        }
    )
    box = wire.hpke_seal(
        recipient_public, "grant", header, plaintext, ephemeral_private=ephemeral_private
    )
    write(
        "hpke_python.json",
        {
            "usage": "grant",
            "recipient_private": schema.b64u_encode(recipient_private),
            "recipient_public": schema.b64u_encode(recipient_public),
            "ephemeral_private": schema.b64u_encode(ephemeral_private),
            "ephemeral_public": schema.b64u_encode(ephemeral_public),
            "header": header,
            "info": "aether/v1/grant",
            "aad": schema.b64u_encode(schema.canonical_bytes(header)),
            "plaintext": schema.b64u_encode(plaintext),
            "box": box,
        },
    )


def ae2() -> None:
    key = bytes(range(32, 64))
    vault_id = bytes(range(16))
    nonce = bytes(range(96, 108))
    plaintext = b'{"text":"hello aether"}'
    blob = wire.ae2_seal(
        key,
        vault_id,
        "memory",
        1,
        "events",
        "payload_enc",
        "42",
        "content",
        plaintext,
        nonce=nonce,
    )
    write(
        "ae2.json",
        {
            "key": schema.b64u_encode(key),
            "vault": schema.b64u_encode(vault_id),
            "cat": "memory",
            "ver": 1,
            "table": "events",
            "column": "payload_enc",
            "row": "42",
            "purpose": "content",
            "nonce": schema.b64u_encode(nonce),
            "plaintext": schema.b64u_encode(plaintext),
            "envelope": schema.b64u_encode(blob),
        },
    )


def recovery() -> None:
    entropy = bytes(range(32))
    vault_id = bytes(range(16))
    keys = wire.recovery_keys(entropy, vault_id)
    write(
        "recovery.json",
        {
            "entropy": schema.b64u_encode(entropy),
            "vault": schema.b64u_encode(vault_id),
            "hpke_private": schema.b64u_encode(keys.hpke_private),
            "hpke_public": schema.b64u_encode(keys.hpke_public),
            "sign_seed": schema.b64u_encode(keys.sign_seed),
            "sign_public": schema.b64u_encode(keys.sign_public),
        },
    )


def wraps() -> None:
    master = bytes(range(32, 64))
    key = bytes(range(64, 96))
    vault_id = bytes(range(16))
    iv = bytes(range(96, 108))
    device = wire.device_wrap(master, vault_id, "memory", 1, key, iv=iv)
    recovery = wire.recovery_keys(bytes(range(32)), vault_id)
    ephemeral = bytes(range(108, 140))
    backup = wire.backup_wrap(
        recovery.hpke_public, vault_id, "memory", 1, key, ephemeral_private=ephemeral
    )
    write(
        "wraps.json",
        {
            "master": schema.b64u_encode(master),
            "vault": schema.b64u_encode(vault_id),
            "cat": "memory",
            "ver": 1,
            "key": schema.b64u_encode(key),
            "iv": schema.b64u_encode(iv),
            "device": device,
            "recovery_private": schema.b64u_encode(recovery.hpke_private),
            "recovery_public": schema.b64u_encode(recovery.hpke_public),
            "ephemeral_private": schema.b64u_encode(ephemeral),
            "backup": backup,
        },
    )


def denial() -> None:
    private_raw = bytes(range(32, 64))
    public_raw = p256_public(private_raw)
    body = {
        "v": 1,
        "type": "lease_denied",
        "vault": schema.b64u_encode(bytes(range(16))),
        "device": schema.b64u_encode(bytes(range(16, 32))),
        "epoch": 2,
        "boot": schema.b64u_encode(bytes(range(32, 48))),
        "task": schema.b64u_encode(bytes(range(48, 64))),
        "request": schema.b64u_encode(bytes(range(64, 80))),
        "nonce": schema.b64u_encode(bytes(range(80, 112))),
        "request_digest": schema.b64u_encode(bytes(range(112, 144))),
        "iat": 1_700_000_300,
        "reason": "locked",
    }
    kid = wire.signer_id(public_raw)
    envelope = wire.sign_body(body, wire.ec_private_key(private_raw), alg="ES256", kid=kid)
    write(
        "denial.json",
        {
            "public": schema.b64u_encode(public_raw),
            "kid": kid,
            "body": body,
            "envelope": envelope,
        },
    )


def errors() -> None:
    cases = [{"status": status, "code": code} for code, status in sorted(ERROR_STATUS.items())]
    write("errors.json", {"cases": cases})


def main() -> None:
    VECTORS.mkdir(parents=True, exist_ok=True)
    jcs()
    es256()
    ed25519()
    hpke()
    ae2()
    recovery()
    wraps()
    denial()
    errors()


if __name__ == "__main__":
    main()
