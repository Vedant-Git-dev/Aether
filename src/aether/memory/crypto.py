"""Application-level encryption at rest.

AES-256-GCM with a server-held key. The trade-off is stated openly: this
protects stored history against a database-level compromise (leaked backup,
breached storage provider), not against a compromise of the server process
itself.

Every encrypted value is bound to its storage location with AES-GCM's
additional-authenticated-data (AAD) — "table:column:record_id" — so a
ciphertext cannot be swapped between records undetected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12
KEY_BYTES = 32
DEDUP_INFO = b"media-dedup"


class CryptoError(Exception):
    """Raised on decryption failure or bad key material."""


def generate_key_b64() -> str:
    """A fresh 32-byte key, base64-encoded — for AETHER_ENCRYPTION_KEY."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


class Cipher:
    """Encrypts/decrypts text and JSON payloads with AES-256-GCM.

    Wire format of stored blobs: nonce(12B) || ciphertext+tag.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
        self._aes = AESGCM(key)
        # a real subkey for the keyed content tags — never the raw AES key
        # reused as an HMAC key, so a leaked dedup tag says nothing about
        # the encryption key and cannot be used to decrypt anything
        self._dedup_key = HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=DEDUP_INFO
        ).derive(key)

    @classmethod
    def from_b64(cls, key_b64: str) -> Cipher:
        try:
            raw = base64.b64decode(key_b64.strip(), validate=True)
        except Exception as exc:  # binascii.Error, ValueError
            raise CryptoError(f"invalid base64 encryption key: {exc}") from exc
        return cls(raw)

    def encrypt_bytes(self, plaintext: bytes, aad: str) -> bytes:
        """Raw bytes in, sealed blob out — the base every other method
        builds on. Callers with binary payloads (packed vectors) use this
        directly instead of paying JSON's size penalty."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = self._aes.encrypt(nonce, plaintext, aad.encode("utf-8"))
        return nonce + ct

    def decrypt_bytes(self, blob: bytes, aad: str) -> bytes:
        if len(blob) <= NONCE_BYTES:
            raise CryptoError("ciphertext too short")
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            return self._aes.decrypt(nonce, ct, aad.encode("utf-8"))
        except InvalidTag as exc:
            raise CryptoError(
                f"decryption failed (wrong key, tampered data, or wrong AAD: {aad!r})"
            ) from exc

    def encrypt_text(self, plaintext: str, aad: str) -> bytes:
        return self.encrypt_bytes(plaintext.encode("utf-8"), aad)

    def dedup_mac(self, data: bytes) -> bytes:
        """A keyed content tag for dedup — same bytes, same tag; different
        bytes, different tag. Deliberately NOT a bare hash: a tag stored in
        a shared database must not let a reader confirm that some known
        file is stored here, and the HMAC key is an HKDF subkey, not the
        AES key, so tags never leak key material either."""
        return hmac.new(self._dedup_key, data, hashlib.sha256).digest()

    def decrypt_text(self, blob: bytes, aad: str) -> str:
        return self.decrypt_bytes(blob, aad).decode("utf-8")

    def encrypt_json(self, obj: object, aad: str) -> bytes:
        return self.encrypt_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), aad)

    def decrypt_json(self, blob: bytes, aad: str) -> Any:
        """json.loads — whatever shape was encrypted, as loose as it was
        stored; the caller knows its section's shape."""
        return json.loads(self.decrypt_text(blob, aad))
