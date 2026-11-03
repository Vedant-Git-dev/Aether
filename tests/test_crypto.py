from __future__ import annotations

import base64

import pytest

from aether.memory.crypto import Cipher, CryptoError, generate_key_b64


@pytest.fixture()
def cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def test_generated_keys_are_32_bytes() -> None:
    c = Cipher.from_b64(generate_key_b64())
    assert isinstance(c, Cipher)


def test_text_roundtrip(cipher: Cipher) -> None:
    blob = cipher.encrypt_text("hello aether", aad="events:payload_enc:42")
    assert isinstance(blob, bytes)
    assert cipher.decrypt_text(blob, aad="events:payload_enc:42") == "hello aether"


def test_json_roundtrip(cipher: Cipher) -> None:
    obj = {"b": [1, 2, 3], "a": "text", "nested": {"ok": True}}
    blob = cipher.encrypt_json(obj, aad="scheduled_actions:payload_enc:7")
    assert cipher.decrypt_json(blob, aad="scheduled_actions:payload_enc:7") == obj


def test_json_roundtrip_keeps_key_order(cipher: Cipher) -> None:
    # an approval's fields read in the order the request carried them — a
    # sealed dict must not come back alphabetized, or every card would
    # show body before recipient before subject
    obj = {"body": "the whole message body", "subject": "Re: Thursday", "to": "sam@example.com"}
    blob = cipher.encrypt_json(obj, aad="pending_approvals:params_enc:9")
    assert list(cipher.decrypt_json(blob, aad="pending_approvals:params_enc:9")) == [
        "body",
        "subject",
        "to",
    ]


def test_non_deterministic_nonce(cipher: Cipher) -> None:
    a = cipher.encrypt_text("same text", aad="t:c:1")
    b = cipher.encrypt_text("same text", aad="t:c:1")
    assert a != b  # fresh nonce every time


def test_tampered_ciphertext_rejected(cipher: Cipher) -> None:
    blob = bytearray(cipher.encrypt_text("payload", aad="t:c:1"))
    blob[-1] ^= 0xFF  # flip a bit in the GCM tag
    with pytest.raises(CryptoError):
        cipher.decrypt_text(bytes(blob), aad="t:c:1")


def test_wrong_aad_rejected(cipher: Cipher) -> None:
    blob = cipher.encrypt_text("payload", aad="events:payload_enc:1")
    with pytest.raises(CryptoError):
        # the record-swap attack: valid ciphertext placed on another record
        cipher.decrypt_text(blob, aad="events:payload_enc:2")


def test_truncated_blob_rejected(cipher: Cipher) -> None:
    with pytest.raises(CryptoError):
        cipher.decrypt_text(b"\x00" * 5, aad="t:c:1")


def test_wrong_key_rejected() -> None:
    blob = Cipher.from_b64(generate_key_b64()).encrypt_text("x", aad="t:c:1")
    other = Cipher.from_b64(generate_key_b64())
    with pytest.raises(CryptoError):
        other.decrypt_text(blob, aad="t:c:1")


def test_bad_key_material_rejected() -> None:
    with pytest.raises(CryptoError):
        Cipher.from_b64("definitely-not-base64!!!")


def test_unicode_roundtrip(cipher: Cipher) -> None:
    text = "héllo — 世界 𠀋 — tabs\tand\nnewlines"  # 𠀋: 4-byte utf-8, emoji-free
    blob = cipher.encrypt_text(text, aad="t:c:1")
    assert cipher.decrypt_text(blob, aad="t:c:1") == text


def test_dedup_mac_is_deterministic_and_content_keyed(cipher: Cipher) -> None:
    a = cipher.dedup_mac(b"same bytes")
    b = cipher.dedup_mac(b"same bytes")
    other = cipher.dedup_mac(b"different bytes")
    assert a == b  # same bytes, same tag — dedup works
    assert a != other  # different bytes, different tag


def test_dedup_mac_is_not_a_bare_hash(cipher: Cipher) -> None:
    import hashlib

    data = b"the media bytes"
    assert cipher.dedup_mac(data) != hashlib.sha256(data).digest()
    assert cipher.dedup_mac(data) != hashlib.md5(data).digest()


def test_dedup_mac_key_depends_on_the_secret() -> None:
    one = Cipher.from_b64(generate_key_b64())
    two = Cipher.from_b64(generate_key_b64())
    data = b"the media bytes"
    assert one.dedup_mac(data) != two.dedup_mac(data)  # a leaked tag names no file


# -- the versioned envelope -------------------------------------------------


def test_new_blobs_carry_the_envelope(cipher: Cipher) -> None:
    from aether.memory.crypto import ENVELOPE_MAGIC

    blob = cipher.encrypt_text("hello", aad="t:c:1")
    assert blob.startswith(ENVELOPE_MAGIC)


def test_wrong_key_version_fails_closed() -> None:
    key = b"k" * 32
    one = Cipher(key, key_id="2026-10")
    two = Cipher(key, key_id="2026-11")
    blob = one.encrypt_text("payload", aad="t:c:1")
    # two only holds 2026-11; the envelope names 2026-10, so it must refuse
    # rather than try its own key and report a generic tag failure
    with pytest.raises(CryptoError, match="no encryption key"):
        two.decrypt_text(blob, aad="t:c:1")


def test_tampering_with_the_purpose_fails(cipher: Cipher) -> None:
    blob = bytearray(cipher.encrypt_text("payload", aad="t:c:1"))
    # the purpose sits after magic, a key-id length + key-id, and a length
    pos = len(b"AE1\x00")
    key_len = blob[pos]
    purpose_len_at = pos + 1 + key_len
    purpose_at = purpose_len_at + 1
    assert blob[purpose_at : purpose_at + 7] == b"content"
    blob[purpose_at : purpose_at + 7] = b"work___"  # same length, different purpose
    with pytest.raises(CryptoError):
        cipher.decrypt_text(bytes(blob), aad="t:c:1")


def test_purpose_subkeys_are_independent() -> None:
    cipher = Cipher(b"p" * 32)
    a = cipher.encrypt_text("same", aad="t:c:1", purpose="content")
    b = cipher.encrypt_text("same", aad="t:c:1", purpose="workspace")
    assert a != b
    assert cipher.decrypt_text(a, aad="t:c:1") == "same"
    assert cipher.decrypt_text(b, aad="t:c:1") == "same"


def test_legacy_blob_still_decrypts() -> None:
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = b"l" * 32
    nonce = secrets.token_bytes(12)
    legacy = nonce + AESGCM(key).encrypt(nonce, b"old payload", b"events:payload_enc:1")
    cipher = Cipher(key)
    assert cipher.decrypt_bytes(legacy, aad="events:payload_enc:1") == b"old payload"


def test_previous_key_reads_old_rows_during_rotation() -> None:
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    old_key, new_key = b"o" * 32, b"n" * 32
    nonce = secrets.token_bytes(12)
    legacy = nonce + AESGCM(old_key).encrypt(nonce, b"old", b"t:c:1")
    rotated = Cipher(new_key, key_id="2026-11", previous={"2026-10": old_key})
    assert rotated.decrypt_bytes(legacy, aad="t:c:1") == b"old"
    # and a v1 blob from the previous key still opens
    old_cipher = Cipher(old_key, key_id="2026-10")
    from_old = old_cipher.encrypt_text("older still", aad="t:c:9")
    assert rotated.decrypt_text(from_old, aad="t:c:9") == "older still"


def test_duplicate_previous_key_id_rejected() -> None:
    with pytest.raises(CryptoError, match="duplicates"):
        Cipher(b"x" * 32, key_id="k1", previous={"k1": b"y" * 32})


def test_bad_key_id_rejected() -> None:
    with pytest.raises(CryptoError, match="key id"):
        Cipher(b"x" * 32, key_id="has spaces")


def test_from_b64_requires_a_previous_key_id() -> None:
    with pytest.raises(CryptoError, match="needs its id"):
        Cipher.from_b64(generate_key_b64(), previous_b64=generate_key_b64())


# -- blind indexes ----------------------------------------------------------


def test_blind_token_is_deterministic_and_case_sensitive(cipher: Cipher) -> None:
    a = cipher.blind_token("handle", "friend@example.com")
    b = cipher.blind_token("handle", "friend@example.com")
    assert a == b
    assert cipher.blind_token("handle", "Friend@example.com") != a


def test_blind_token_purpose_isolation(cipher: Cipher) -> None:
    assert cipher.blind_token("handle", "x") != cipher.blind_token("name", "x")


def test_blind_token_depends_on_the_secret() -> None:
    one = Cipher(b"1" * 32)
    two = Cipher(b"2" * 32)
    assert one.blind_token("handle", "x") != two.blind_token("handle", "x")


def test_blind_token_is_not_a_bare_hash(cipher: Cipher) -> None:
    import hashlib

    value = "friend@example.com"
    assert cipher.blind_token("handle", value) != hashlib.sha256(value.encode()).digest()


def test_blind_token_rejects_bad_purpose(cipher: Cipher) -> None:
    with pytest.raises(CryptoError, match="purpose"):
        cipher.blind_token("Has Spaces", "x")


# -- wiring ----------------------------------------------------------------


def test_build_cipher_uses_the_configured_key_version() -> None:
    from aether.config import Settings
    from aether.main import _build_cipher

    settings = Settings.without_env_file(
        encryption_key=generate_key_b64(), encryption_key_id="2026-11"
    )
    assert _build_cipher(settings).key_id == "2026-11"


def test_build_cipher_reads_the_previous_key_during_rotation() -> None:
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from aether.config import Settings
    from aether.main import _build_cipher

    old_b64, new_b64 = generate_key_b64(), generate_key_b64()
    old_key = base64.b64decode(old_b64)
    nonce = secrets.token_bytes(12)
    legacy = nonce + AESGCM(old_key).encrypt(nonce, b"old", b"t:c:1")
    settings = Settings.without_env_file(
        encryption_key=new_b64,
        encryption_key_id="2026-11",
        encryption_key_previous=old_b64,
        encryption_key_previous_id="2026-10",
    )
    assert _build_cipher(settings).decrypt_bytes(legacy, aad="t:c:1") == b"old"
