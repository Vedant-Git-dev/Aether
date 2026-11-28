from __future__ import annotations

import ctypes

import pytest
import reference_wire as wire
from test_wire_vectors import load

from aether.vault import native, native_grant, schema
from aether.vault.errors import VaultProtocolError
from aether.vault.scope import VaultClosed, VaultLocked


@pytest.fixture
def boot():
    try:
        native._sodium()
        native_grant._openssl()
    except VaultLocked as exc:
        pytest.skip(str(exc))
    recipient = native_grant.NativeBootKey()
    try:
        yield recipient
    finally:
        recipient.close()


@pytest.mark.parametrize("fixture", ["hpke_python.json", "kotlin/hpke_kotlin.json"])
def test_native_grant_opens_cross_language_vector(boot, fixture):
    vector = load(fixture)
    private = schema.b64u_decode(vector["recipient_private"], field="recipient_private")
    ctypes.memmove(boot._private.address, private, len(private))
    slots = boot.open_grant_slots(vector["header"] | {"box": vector["box"]})
    keys = schema.loads_strict(schema.b64u_decode(vector["plaintext"], field="plaintext"))
    vault = schema.b64u_decode(vector["header"]["vault"], field="vault")
    try:
        assert tuple(slots) == (("memory", 1), ("operations", 1))
        for entry in keys["keys"]:
            cat, ver = entry["cat"], entry["ver"]
            handle = native.NativeKeyHandle(vault_id=vault, cat=cat, ver=ver, slot=slots[cat, ver])
            key = schema.b64u_decode(entry["key"], field="key")
            assert handle.token("test", b"vector") == wire.token_value(
                key, vault, cat, ver, "test", b"vector"
            )
    finally:
        for slot in slots.values():
            slot.close()


def test_grant_is_bound_to_header_and_live_boot(boot):
    vector = load("hpke_python.json")
    header = vector["header"]
    plaintext = schema.b64u_decode(vector["plaintext"], field="plaintext")
    body = header | {"box": wire.hpke_seal(boot.public, "grant", header, plaintext)}
    for slot in boot.open_grant_slots(body).values():
        slot.close()
    with pytest.raises(VaultProtocolError, match="HPKE authentication failed"):
        boot.open_grant_slots(body | {"epoch": header["epoch"] + 1})
    other = native_grant.NativeBootKey()
    try:
        with pytest.raises(VaultProtocolError, match="HPKE authentication failed"):
            other.open_grant_slots(body)
    finally:
        other.close()
    boot.close()
    with pytest.raises(VaultClosed):
        boot.open_grant_slots(body)


def test_grant_rejects_noncanonical_key_and_wipes_partial_slots(boot, monkeypatch):
    vector = load("hpke_python.json")
    header = vector["header"]
    plaintext = schema.loads_strict(schema.b64u_decode(vector["plaintext"], field="plaintext"))
    second_key = plaintext["keys"][1]["key"]
    # The final base64url symbol for 32 bytes has two unused bits.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    plaintext["keys"][1]["key"] = second_key[:-1] + alphabet[alphabet.index(second_key[-1]) + 1]
    box = wire.hpke_seal(boot.public, "grant", header, schema.canonical_bytes(plaintext))

    class ObservedSlot(native._KeySlot):
        instances = []

        def __init__(self, *args):
            super().__init__(*args)
            self.instances.append(self)

    monkeypatch.setattr(native_grant, "_KeySlot", ObservedSlot)
    with pytest.raises(VaultProtocolError, match="invalid category key"):
        boot.open_grant_slots(header | {"box": box})
    assert len(ObservedSlot.instances) == 3
    for slot in ObservedSlot.instances:
        with pytest.raises(VaultClosed):
            _ = slot.address
