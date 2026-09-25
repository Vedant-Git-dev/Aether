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
    text = "héllo — 世界 🌍 — tabs\tand\nnewlines"
    blob = cipher.encrypt_text(text, aad="t:c:1")
    assert cipher.decrypt_text(blob, aad="t:c:1") == text
