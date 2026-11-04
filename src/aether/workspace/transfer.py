"""Passphrase-encrypted workspace export and import.

The bundle is a tiny self-describing envelope:

    AETHWSP1 | salt (16) | nonce (12) | AES-256-GCM ciphertext

The key is scrypt(passphrase, salt) with parameters sized for one file on a
phone, not for a password database at scale. The plaintext is JSON
`{"version": 1, "files": {name: text}}`. Nothing here touches the database;
the route layer reads the sealed rows into memory, calls `export_bundle`,
and streams the bytes back. Import is the reverse.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..memory.crypto import CryptoError

MAGIC = b"AETHWSP1"
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
# ~16 MiB of memory per guess; enough friction for a backup file, still
# instant on a phone
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise CryptoError("a passphrase is required")
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=KEY_BYTES,
    )


def export_bundle(files: dict[str, str], passphrase: str) -> bytes:
    """Seal the workspace files under a passphrase. The caller is
    responsible for having already rejected anything that should never be
    exported; this is a faithful copy of what the user can read."""
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    payload = json.dumps(
        {"version": 1, "files": files},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(_key(passphrase, salt)).encrypt(nonce, payload, MAGIC + salt)
    return MAGIC + salt + nonce + ciphertext


def import_bundle(data: bytes, passphrase: str) -> dict[str, str]:
    """Open a bundle. Raises CryptoError for a wrong passphrase, a truncated
    file or a tampered header — the caller turns that into one plain
    sentence with no detail about which check failed."""
    if not data.startswith(MAGIC) or len(data) <= len(MAGIC) + SALT_BYTES + NONCE_BYTES:
        raise CryptoError("this is not an Aether workspace export")
    body = data[len(MAGIC) :]
    salt, nonce = body[:SALT_BYTES], body[SALT_BYTES : SALT_BYTES + NONCE_BYTES]
    ciphertext = body[SALT_BYTES + NONCE_BYTES :]
    try:
        plaintext = AESGCM(_key(passphrase, salt)).decrypt(nonce, ciphertext, MAGIC + salt)
    except (InvalidTag, ValueError) as exc:
        raise CryptoError("the passphrase did not open this export") from exc
    try:
        payload: Any = json.loads(plaintext)
        files = payload["files"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CryptoError("the export is not readable") from exc
    if not isinstance(files, dict):
        raise CryptoError("the export is not readable")
    out: dict[str, str] = {}
    for name, text in files.items():
        if not isinstance(name, str) or not isinstance(text, str):
            raise CryptoError("the export is not readable")
        out[name] = text
    return out
