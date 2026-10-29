"""The provider-token keyring: AES-GCM, key ids in the AAD, rotation."""

from __future__ import annotations

import pytest

from aether.memory.crypto import Cipher, CryptoError, generate_key_b64
from aether.providers.crypto import TokenKeyError, TokenKeyring


def _keyring(active_id: str = "v2", previous_id: str = "v1") -> tuple[TokenKeyring, str]:
    active = generate_key_b64()
    previous = generate_key_b64()
    ring = TokenKeyring(
        active_id=active_id,
        active_key_b64=active,
        previous_id=previous_id,
        previous_key_b64=previous,
    )
    return ring, previous


def test_no_active_key_means_no_keyring() -> None:
    assert TokenKeyring.from_values(active_key_b64="", active_id="") is None


def test_previous_key_without_active_key_is_an_error() -> None:
    with pytest.raises(TokenKeyError):
        TokenKeyring.from_values(
            active_key_b64="",
            active_id="",
            previous_key_b64=generate_key_b64(),
            previous_id="v1",
        )


def test_active_key_needs_an_id() -> None:
    with pytest.raises(TokenKeyError):
        TokenKeyring.from_values(active_key_b64=generate_key_b64(), active_id="")


def test_encrypt_and_decrypt_bind_to_the_key_id_and_aad() -> None:
    ring, _ = _keyring()
    blob, key_id = ring.encrypt_text("secret", "provider_connections:access_enc:gmail")
    assert key_id == "v2"
    assert ring.decrypt_text(blob, "provider_connections:access_enc:gmail", key_id) == "secret"
    with pytest.raises(CryptoError):
        ring.decrypt_text(blob, "provider_connections:access_enc:github", key_id)
    with pytest.raises(TokenKeyError):
        ring.decrypt_text(blob, "provider_connections:access_enc:gmail", "unknown")


def test_previous_key_decrypts_old_ciphertext() -> None:
    ring, previous = _keyring()
    old_blob = Cipher.from_b64(previous).encrypt_text(
        "old", "provider_connections:access_enc:gmail:v1"
    )
    assert ring.decrypt_text(old_blob, "provider_connections:access_enc:gmail", "v1") == "old"


def test_json_round_trip() -> None:
    ring, _ = _keyring()
    blob, key_id = ring.encrypt_json({"a": 1}, "provider_oauth_states:code_verifier_enc:s")
    assert ring.decrypt_json(blob, "provider_oauth_states:code_verifier_enc:s", key_id) == {"a": 1}
