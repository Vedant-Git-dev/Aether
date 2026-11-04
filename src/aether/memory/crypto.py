"""Application-level encryption at rest.

AES-256-GCM with a server-held key. The trade-off is stated openly: this
protects stored history against a database-level compromise (leaked backup,
breached storage provider), not against a compromise of the server process
itself.

Every encrypted value is bound to its storage location with AES-GCM's
additional-authenticated-data (AAD) — "table:column:record_id" — so a
ciphertext cannot be swapped between records undetected.

Sealed values now carry a small self-describing envelope:

    AE1\\x00 | key-id length | key-id | purpose length | purpose |
    nonce (12B) | ciphertext + tag

The envelope names the key version and the purpose subkey, and both ride
the AAD. A blob written under one version cannot be read as another, and
the same master key can serve independent purposes (content, workspace,
blind indexes) without reusing one key for two jobs. Blobs written by the
older code — a bare nonce followed by ciphertext — still decrypt, so a
deployment does not have to re-encrypt history before booting. Re-encrypting
those rows lazily upgrades them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12
KEY_BYTES = 32
DEDUP_INFO = b"media-dedup"

# the versioned envelope's magic. Legacy blobs are a bare nonce + ciphertext;
# the four-byte mark makes the two shapes unambiguous.
ENVELOPE_MAGIC = b"AE1\x00"
KEY_ID_BYTES = 64
PURPOSE_BYTES = 32
PURPOSE_CONTENT = "content"

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PURPOSE_RE = re.compile(r"^[a-z0-9_.:-]{1,32}$")


class CryptoError(Exception):
    """Raised on decryption failure or bad key material."""


def generate_key_b64() -> str:
    """A fresh 32-byte key, base64-encoded — for AETHER_ENCRYPTION_KEY."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _decode_key(key_b64: str, label: str) -> bytes:
    try:
        raw = base64.b64decode(key_b64.strip(), validate=True)
    except Exception as exc:  # binascii.Error, ValueError
        raise CryptoError(f"invalid base64 {label}: {exc}") from exc
    if len(raw) != KEY_BYTES:
        raise CryptoError(f"{label} must decode to {KEY_BYTES} bytes, got {len(raw)}")
    return raw


class Cipher:
    """Encrypts/decrypts text and JSON payloads with AES-256-GCM.

    Wire formats understood on read:
      v1 (this class): AE1 envelope, purpose subkey, key-id in the AAD
      v0 (legacy):     nonce(12B) || ciphertext+tag, master key, bare AAD
    New writes are always v1.
    """

    def __init__(
        self,
        key: bytes,
        *,
        key_id: str = "k1",
        previous: dict[str, bytes] | None = None,
    ) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
        _check_key_id(key_id)
        self._keys: dict[str, bytes] = {key_id: bytes(key)}
        for previous_id, previous_key in (previous or {}).items():
            _check_key_id(previous_id)
            if previous_id == key_id:
                raise CryptoError(f"previous key id {previous_id!r} duplicates the active id")
            if len(previous_key) != KEY_BYTES:
                raise CryptoError(
                    f"previous key {previous_id!r} must be {KEY_BYTES} bytes, "
                    f"got {len(previous_key)}"
                )
            self._keys[previous_id] = bytes(previous_key)
        self._active_id = key_id
        self._subkeys: dict[tuple[str, str], bytes] = {}
        # a real subkey for the keyed content tags — never the raw AES key
        # reused as an HMAC key, so a leaked dedup tag says nothing about
        # the encryption key and cannot be used to decrypt anything
        self._dedup_key = HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=DEDUP_INFO
        ).derive(key)

    @classmethod
    def from_b64(
        cls,
        key_b64: str,
        *,
        key_id: str = "k1",
        previous_b64: str = "",
        previous_id: str = "",
    ) -> Cipher:
        raw = _decode_key(key_b64, "encryption key")
        previous: dict[str, bytes] = {}
        if previous_b64.strip():
            if not previous_id.strip():
                raise CryptoError("a previous encryption key needs its id")
            previous[previous_id.strip()] = _decode_key(previous_b64, "previous encryption key")
        elif previous_id.strip():
            raise CryptoError("a previous encryption key id was given without the key")
        return cls(raw, key_id=key_id, previous=previous)

    @property
    def key_id(self) -> str:
        """The active key version — written into every new envelope."""
        return self._active_id

    # -- the envelope -------------------------------------------------------

    def encrypt_bytes(
        self,
        plaintext: bytes,
        aad: str,
        *,
        purpose: str = PURPOSE_CONTENT,
    ) -> bytes:
        """Raw bytes in, sealed v1 blob out — the base every other method
        builds on. Callers with binary payloads (packed vectors) use this
        directly instead of paying JSON's size penalty."""
        key_id = self._active_id
        nonce = secrets.token_bytes(NONCE_BYTES)
        key = self._subkey(key_id, purpose)
        ct = AESGCM(key).encrypt(
            nonce, plaintext, _envelope_aad(aad, key_id, purpose).encode("utf-8")
        )
        key_bytes = key_id.encode("utf-8")
        purpose_bytes = purpose.encode("utf-8")
        return (
            ENVELOPE_MAGIC
            + bytes([len(key_bytes)])
            + key_bytes
            + bytes([len(purpose_bytes)])
            + purpose_bytes
            + nonce
            + ct
        )

    def decrypt_bytes(self, blob: bytes, aad: str) -> bytes:
        if blob.startswith(ENVELOPE_MAGIC):
            key_id, purpose, nonce, ct = _parse_envelope(blob)
            key = self._subkey(key_id, purpose)
            try:
                return AESGCM(key).decrypt(
                    nonce, ct, _envelope_aad(aad, key_id, purpose).encode("utf-8")
                )
            except InvalidTag as exc:
                raise CryptoError(
                    f"decryption failed (wrong key, tampered data, or wrong AAD: {aad!r})"
                ) from exc
        # v0 — a bare nonce + ciphertext under the master key. Try the
        # active key first, then previous keys during a rotation window.
        if len(blob) <= NONCE_BYTES:
            raise CryptoError("ciphertext too short")
        nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        for key_id in self._keys:
            try:
                return AESGCM(self._keys[key_id]).decrypt(nonce, ct, aad.encode("utf-8"))
            except InvalidTag:
                continue
        raise CryptoError(f"decryption failed (wrong key, tampered data, or wrong AAD: {aad!r})")

    def encrypt_text(self, plaintext: str, aad: str, *, purpose: str = PURPOSE_CONTENT) -> bytes:
        return self.encrypt_bytes(plaintext.encode("utf-8"), aad, purpose=purpose)

    def decrypt_text(self, blob: bytes, aad: str) -> str:
        return self.decrypt_bytes(blob, aad).decode("utf-8")

    def encrypt_json(self, obj: object, aad: str, *, purpose: str = PURPOSE_CONTENT) -> bytes:
        # no sort_keys: a dict's own key order is part of what the user
        # reads back — an approval's fields render in the order the
        # request carried them, not alphabetized. Digests that need
        # canonical bytes canonicalize themselves; AEAD needs none.
        return self.encrypt_text(json.dumps(obj, ensure_ascii=False), aad, purpose=purpose)

    def decrypt_json(self, blob: bytes, aad: str) -> Any:
        """json.loads — whatever shape was encrypted, as loose as it was
        stored; the caller knows its section's shape."""
        return json.loads(self.decrypt_text(blob, aad))

    # -- keyed indexes ------------------------------------------------------

    def dedup_mac(self, data: bytes) -> bytes:
        """A keyed content tag for dedup — same bytes, same tag; different
        bytes, different tag. Deliberately NOT a bare hash: a tag stored in
        a shared database must not let a reader confirm that some known
        file is stored here, and the HMAC key is an HKDF subkey, not the
        AES key, so tags never leak key material either."""
        return hmac.new(self._dedup_key, data, hashlib.sha256).digest()

    def blind_token(self, purpose: str, value: str | bytes) -> bytes:
        """An equality-only lookup token for one value under one purpose.

        The key is an HKDF subkey of its own, and the purpose is part of
        the derivation, so a token for an email address cannot be compared
        with a token for a display name, and a different master key yields
        different tokens. Callers pass an already-normalized value — this
        function does not guess at case or whitespace rules.
        """
        _check_purpose(purpose)
        if isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raise TypeError(f"blind_token needs str or bytes, got {type(value).__name__}")
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_BYTES,
            salt=self._active_id.encode("utf-8"),
            info=b"aether-blind:" + purpose.encode("utf-8"),
        ).derive(self._keys[self._active_id])
        return hmac.new(key, raw, hashlib.sha256).digest()

    # -- internals ----------------------------------------------------------

    def _subkey(self, key_id: str, purpose: str) -> bytes:
        _check_purpose(purpose)
        if key_id not in self._keys:
            # fail closed: a blob naming a key this process does not hold
            # is a key-version problem, never a reason to try another key
            raise CryptoError(f"no encryption key with id {key_id!r}")
        cache_key = (key_id, purpose)
        cached = self._subkeys.get(cache_key)
        if cached is None:
            cached = HKDF(
                algorithm=hashes.SHA256(),
                length=KEY_BYTES,
                salt=key_id.encode("utf-8"),
                info=b"aether-purpose:" + purpose.encode("utf-8"),
            ).derive(self._keys[key_id])
            self._subkeys[cache_key] = cached
        return cached


def _check_key_id(key_id: str) -> None:
    if not _KEY_ID_RE.match(key_id or ""):
        raise CryptoError("key id must be 1-64 chars of letters, digits, dot, underscore or dash")


def _check_purpose(purpose: str) -> None:
    if not _PURPOSE_RE.match(purpose or ""):
        raise CryptoError(
            "purpose must be 1-32 lowercase chars of letters, digits, dot, colon, "
            "underscore or dash"
        )


def _envelope_aad(aad: str, key_id: str, purpose: str) -> str:
    return f"{aad}|key:{key_id}|purpose:{purpose}"


def _parse_envelope(blob: bytes) -> tuple[str, str, bytes, bytes]:
    """Magic already matched. Returns (key_id, purpose, nonce, ciphertext)."""
    pos = len(ENVELOPE_MAGIC)
    if len(blob) < pos + 2:
        raise CryptoError("sealed envelope is truncated")
    key_len = blob[pos]
    pos += 1
    if key_len == 0 or key_len > KEY_ID_BYTES or len(blob) < pos + key_len + 1:
        raise CryptoError("sealed envelope has an invalid key id")
    key_id = blob[pos : pos + key_len].decode("utf-8", errors="replace")
    pos += key_len
    purpose_len = blob[pos]
    pos += 1
    if purpose_len == 0 or purpose_len > PURPOSE_BYTES or len(blob) < pos + purpose_len:
        raise CryptoError("sealed envelope has an invalid purpose")
    purpose = blob[pos : pos + purpose_len].decode("utf-8", errors="replace")
    pos += purpose_len
    if len(blob) <= pos + NONCE_BYTES:
        raise CryptoError("ciphertext too short")
    return key_id, purpose, blob[pos : pos + NONCE_BYTES], blob[pos + NONCE_BYTES :]
