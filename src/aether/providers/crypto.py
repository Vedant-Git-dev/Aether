"""The provider-token keyring.

Provider tokens use their own AES-256-GCM key, separate from the content
key. Every ciphertext records the key id that sealed it; the id is also in
the AAD, so a row cannot be moved to another key version without failing
authentication. A rotation can therefore decrypt with the old key and
re-encrypt under the active one without touching any other table.
"""

from __future__ import annotations

from typing import Any

from ..memory.crypto import Cipher, CryptoError


class TokenKeyError(ValueError):
    """The token keyring is missing or malformed."""


class TokenKeyring:
    """Active key plus, during a rotation, one previous key."""

    def __init__(
        self,
        *,
        active_id: str,
        active_key_b64: str,
        previous_id: str = "",
        previous_key_b64: str = "",
    ) -> None:
        active_id = active_id.strip()
        if not active_id:
            raise TokenKeyError("AETHER_TOKEN_ENCRYPTION_KEY_ID is required")
        try:
            active = Cipher.from_b64(active_key_b64)
        except CryptoError as exc:
            raise TokenKeyError(f"invalid provider token key: {exc}") from exc
        self._active_id = active_id
        self._ciphers: dict[str, Cipher] = {active_id: active}

        if previous_key_b64 or previous_id:
            previous_id = previous_id.strip()
            if not previous_id:
                raise TokenKeyError(
                    "AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS_ID is required when a previous key is set"
                )
            if not previous_key_b64:
                raise TokenKeyError(
                    "AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS is required when a previous id is set"
                )
            if previous_id == active_id:
                raise TokenKeyError("the active and previous provider token key ids must differ")
            try:
                self._ciphers[previous_id] = Cipher.from_b64(previous_key_b64)
            except CryptoError as exc:
                raise TokenKeyError(f"invalid previous provider token key: {exc}") from exc

    @classmethod
    def from_values(
        cls,
        *,
        active_key_b64: str,
        active_id: str,
        previous_key_b64: str = "",
        previous_id: str = "",
    ) -> TokenKeyring | None:
        """Build a keyring, or None when no token key is configured.

        A previous key without an active key is a configuration error: it
        would make provider connections impossible while looking configured.
        """
        active_key_b64 = active_key_b64.strip()
        if not active_key_b64:
            if previous_key_b64.strip() or previous_id.strip():
                raise TokenKeyError(
                    "a previous provider token key is set but the active key is missing"
                )
            return None
        return cls(
            active_id=active_id,
            active_key_b64=active_key_b64,
            previous_id=previous_id,
            previous_key_b64=previous_key_b64,
        )

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._ciphers)

    def encrypt_text(self, plaintext: str, aad_prefix: str) -> tuple[bytes, str]:
        key_id = self._active_id
        return self._ciphers[key_id].encrypt_text(plaintext, f"{aad_prefix}:{key_id}"), key_id

    def decrypt_text(self, blob: bytes, aad_prefix: str, key_id: str) -> str:
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise TokenKeyError(f"no provider token key named {key_id!r} is configured")
        return cipher.decrypt_text(blob, f"{aad_prefix}:{key_id}")

    def encrypt_json(self, obj: Any, aad_prefix: str) -> tuple[bytes, str]:
        key_id = self._active_id
        return self._ciphers[key_id].encrypt_json(obj, f"{aad_prefix}:{key_id}"), key_id

    def decrypt_json(self, blob: bytes, aad_prefix: str, key_id: str) -> Any:
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise TokenKeyError(f"no provider token key named {key_id!r} is configured")
        return cipher.decrypt_json(blob, f"{aad_prefix}:{key_id}")
