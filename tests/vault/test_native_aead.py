from __future__ import annotations

import ctypes
import mmap
from dataclasses import replace

import pytest
import reference_wire as wire
from test_wire_vectors import load

from aether.memory.crypto import Cipher
from aether.memory.storage import ScopedStore
from aether.vault import native, schema
from aether.vault.errors import VaultProtocolError
from aether.vault.scope import (
    KeyScope,
    LegacyCiphertextError,
    Location,
    ReservedWrites,
    VaultClosed,
    VaultExhausted,
    VaultFormatError,
    VaultLocked,
)


@pytest.fixture
def native_slot():
    try:
        native._sodium()
    except VaultLocked as exc:
        pytest.skip(str(exc))
    slot = native._KeySlot()
    try:
        yield slot
    finally:
        slot.close()


def test_native_aead_matches_public_vector_and_scrubs_derived_state(native_slot, monkeypatch):
    fixture = load("ae2.json")
    location = Location(**{field: fixture[field] for field in Location.__dataclass_fields__})
    vault, key, nonce, plaintext, blob = [
        schema.b64u_decode(fixture[field], field=field)
        for field in ("vault", "key", "nonce", "plaintext", "envelope")
    ]
    # Test data is public. The production grant importer will write the slot in native code.
    ctypes.memmove(native_slot.address, key, len(key))
    monkeypatch.setattr(native, "token_bytes", lambda _: nonce)
    ref = (location.cat, location.ver)
    handle = native.NativeKeyHandle(
        vault_id=vault,
        cat=ref[0],
        ver=ref[1],
        slot=native_slot,
        writes=ReservedWrites(vault, *ref, count=1),
    )
    with KeyScope(vault_id=vault, task=b"T" * 16, operation="turn", handles={ref: handle}) as scope:
        store = ScopedStore(scope, cat=location.cat, ver=location.ver)
        assert store.unseal(
            blob, table=location.table, column=location.column, row_id=location.row
        ) == {"text": "hello aether"}
        assert scope.seal(location, plaintext) == blob
        with pytest.raises(VaultExhausted):
            scope.seal(location, plaintext)
        for moved in (
            replace(location, row="43"),
            replace(location, column="other_enc"),
            replace(location, purpose="alternate"),
            replace(location, table="other"),
        ):
            with pytest.raises(VaultProtocolError, match="authentication failed"):
                scope.open(moved, blob)
        with pytest.raises(VaultFormatError):
            scope.open(location, blob + b" ")
        with pytest.raises(LegacyCiphertextError):
            scope.open(location, b"AE1\0sample")
        assert scope.token("event.source", "sample") == wire.token_value(
            key, vault, *ref, "events.source", b"sample"
        )
        assert scope.dedup("event.dedup", b"sample") == wire.dedup_value(
            key, vault, *ref, "events.dedup", b"sample"
        )
        assert ctypes.string_at(
            native_slot.address + schema.KEY_BYTES, mmap.PAGESIZE - schema.KEY_BYTES
        ) == bytes(mmap.PAGESIZE - schema.KEY_BYTES)
    with pytest.raises(VaultClosed):
        handle.open(location, blob)


def test_native_read_handle_denies_sealing_without_a_reservation(native_slot):
    vault = b"V" * 16
    location = Location("memory", 1, "events", "payload_enc", "42")
    handle = native.NativeKeyHandle(vault_id=vault, cat="memory", ver=1, slot=native_slot)
    with pytest.raises(VaultClosed, match="cannot be transferred"):
        native.NativeKeyHandle(vault_id=vault, cat="memory", ver=1, slot=native_slot)
    with pytest.raises(VaultLocked, match="no reserved writes"):
        handle.seal(location, b"synthetic")


def test_native_keys_refuse_failed_memory_protection(monkeypatch):
    try:
        lib = native._sodium()
    except VaultLocked as exc:
        pytest.skip(str(exc))
    monkeypatch.setattr(lib, "sodium_mlock", lambda *_: -1)
    with pytest.raises(VaultLocked, match="could not lock native vault memory"):
        native._KeySlot()


def test_historical_lookup_matches_legacy_tag_and_closes(native_slot):
    legacy = Cipher(bytes(range(32)))
    ctypes.memmove(native_slot.address, legacy._dedup_key, schema.KEY_BYTES)
    handle = native.NativeHistoricalLookup(native_slot)
    assert handle.tag(b"synthetic event") == legacy.dedup_mac(b"synthetic event")
    assert ctypes.string_at(
        native_slot.address + schema.KEY_BYTES, mmap.PAGESIZE - schema.KEY_BYTES
    ) == bytes(mmap.PAGESIZE - schema.KEY_BYTES)
    handle.close()
    with pytest.raises(VaultClosed):
        handle.tag(b"synthetic event")
