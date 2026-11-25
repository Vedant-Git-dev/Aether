"""Open authenticated grant boxes directly into protected native key slots."""

from __future__ import annotations

import ctypes as c
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes.util import find_library
from functools import cache
from typing import Any

from .errors import VaultProtocolError
from .native import _POINTER, _SIZE, _KeySlot
from .schema import (
    GCM_TAG_BYTES,
    KEY_BYTES,
    X25519_BYTES,
    b64u_decode,
    canonical_bytes,
    validate_lease_grant,
    validate_refs,
)
from .scope import VaultClosed, VaultLocked


class _Suite(c.Structure):
    _fields_ = [("kem_id", c.c_uint16), ("kdf_id", c.c_uint16), ("aead_id", c.c_uint16)]


_SUITE = _Suite(0x0020, 0x0001, 0x0002)
_BASE_MODE = 0
_RECEIVER = 1
_INFO = b"aether/v1/grant"
_KEY_TEXT_SIZE = (KEY_BYTES * 8 + 5) // 6
_URLSAFE_NO_PADDING = 7  # libsodium's sodium_base64_VARIANT_URLSAFE_NO_PADDING


@cache
def _openssl() -> c.CDLL:
    path = find_library("crypto")
    if not path:
        raise VaultLocked("native HPKE is unavailable")
    try:
        lib = c.CDLL(path)
        size_out = c.POINTER(_SIZE)
        signatures = {
            "EVP_PKEY_new_raw_private_key_ex": (
                _POINTER,
                _POINTER,
                _POINTER,
                _POINTER,
                _POINTER,
                _SIZE,
            ),
            "EVP_PKEY_get_raw_public_key": (c.c_int, _POINTER, _POINTER, size_out),
            "EVP_PKEY_free": (None, _POINTER),
            "OSSL_HPKE_suite_check": (c.c_int, _Suite),
            "OSSL_HPKE_CTX_new": (_POINTER, c.c_int, _Suite, c.c_int, _POINTER, _POINTER),
            "OSSL_HPKE_CTX_free": (None, _POINTER),
            "OSSL_HPKE_decap": (c.c_int, _POINTER, _POINTER, _SIZE, _POINTER, _POINTER, _SIZE),
            "OSSL_HPKE_open": (
                c.c_int,
                _POINTER,
                _POINTER,
                size_out,
                _POINTER,
                _SIZE,
                _POINTER,
                _SIZE,
            ),
            "ERR_clear_error": (None,),
        }
        for name, (result, *args) in signatures.items():
            function = getattr(lib, name)
            function.argtypes = args
            function.restype = result
        if lib.OSSL_HPKE_suite_check(_SUITE) != 1:
            raise VaultLocked("the vault HPKE suite is unavailable")
    except (OSError, AttributeError) as exc:
        raise VaultLocked("native HPKE is unavailable") from exc
    return lib


def _match(clear: _KeySlot, offset: int, expected: bytes) -> int:
    if clear.lib.sodium_memcmp(clear.address + offset, expected, len(expected)) != 0:
        raise VaultProtocolError("bad_field", "grant keys do not match the requested set")
    return offset + len(expected)


class NativeBootKey:
    """One boot's HPKE recipient. Retire it when leadership ends."""

    def __init__(self) -> None:
        self._lib = _openssl()
        self._private = _KeySlot()
        try:
            self._private.lib.randombytes_buf(self._private.address, X25519_BYTES)
            public = c.create_string_buffer(X25519_BYTES)
            length = _SIZE(len(public))
            with self._recipient() as recipient:
                if self._lib.EVP_PKEY_get_raw_public_key(recipient, public, c.byref(length)) != 1:
                    raise VaultLocked("could not derive the HPKE recipient public key")
            self.public = public.raw
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _recipient(self) -> Iterator[int]:
        if not self._private.owned_by_current_process():
            raise VaultClosed("the HPKE recipient belongs to a different process")
        with self._private.lock:
            recipient = self._lib.EVP_PKEY_new_raw_private_key_ex(
                None, b"X25519", None, self._private.address, X25519_BYTES
            )
            if not recipient:
                self._lib.ERR_clear_error()
                raise VaultLocked("could not load the native HPKE recipient")
            try:
                yield recipient
            finally:
                self._lib.EVP_PKEY_free(recipient)
                self._lib.ERR_clear_error()

    def close(self) -> None:
        self._private.close()

    def open_grant_slots(self, body: dict[str, Any]) -> dict[tuple[str, int], _KeySlot]:
        """Open a previously verified and matched grant. The installer owns returned slots."""
        validate_lease_grant(body)
        refs = validate_refs(body["refs"])
        enc = b64u_decode(body["box"]["enc"], field="box.enc", length=X25519_BYTES)
        ct = b64u_decode(body["box"]["ct"], field="box.ct")
        aad = canonical_bytes({name: value for name, value in body.items() if name != "box"})
        # Build the canonical public scaffolding. The only unexamined spans are
        # fixed-width base64url keys, decoded by libsodium straight into slots.
        entries = [
            canonical_bytes({"cat": cat, "key": "", "ver": ver}).split(b'""', 1)
            for cat, ver in refs
        ]
        prefix, suffix = b'{"keys":[', b"]}"
        size = len(prefix) + sum(
            len(before) + len(after) + _KEY_TEXT_SIZE + 2 for before, after in entries
        )
        size += len(entries) - 1 + len(suffix)
        if len(ct) != size + GCM_TAG_BYTES:
            raise VaultProtocolError("bad_field", "grant key payload has an invalid length")

        clear = _KeySlot(size)
        slots: dict[tuple[str, int], _KeySlot] = {}
        try:
            self._open(enc, ct, aad, clear, size)
            offset = _match(clear, 0, prefix)
            for index, (ref, (before, after)) in enumerate(zip(refs, entries, strict=True)):
                offset = _match(clear, offset, (b"," if index else b"") + before + b'"')
                slot = slots[ref] = _KeySlot()
                decoded_size = _SIZE()
                if (
                    clear.lib.sodium_base642bin(
                        slot.address,
                        KEY_BYTES,
                        clear.address + offset,
                        _KEY_TEXT_SIZE,
                        None,
                        c.byref(decoded_size),
                        None,
                        _URLSAFE_NO_PADDING,
                    )
                    != 0
                    or decoded_size.value != KEY_BYTES
                ):
                    raise VaultProtocolError("bad_field", "grant contains an invalid category key")
                offset = _match(clear, offset + _KEY_TEXT_SIZE, b'"' + after)
            _match(clear, offset, suffix)
            return slots
        except BaseException:
            for slot in slots.values():
                slot.close()
            raise
        finally:
            clear.close()

    def _open(self, enc: bytes, ct: bytes, aad: bytes, clear: _KeySlot, size: int) -> None:
        with self._recipient() as recipient:
            context = self._lib.OSSL_HPKE_CTX_new(_BASE_MODE, _SUITE, _RECEIVER, None, None)
            if not context:
                raise VaultLocked("could not initialize the native HPKE receiver")
            try:
                length = _SIZE(size)
                if (
                    self._lib.OSSL_HPKE_decap(context, enc, len(enc), recipient, _INFO, len(_INFO))
                    != 1
                    or self._lib.OSSL_HPKE_open(
                        context, clear.address, c.byref(length), aad, len(aad), ct, len(ct)
                    )
                    != 1
                    or length.value != size
                ):
                    raise VaultProtocolError("denied", "grant HPKE authentication failed")
            finally:
                self._lib.OSSL_HPKE_CTX_free(context)


class NativeIngressKey:
    """A leased ingress private key with its own protected slot."""

    cat = "ingress"

    def __init__(self, ver: int, slot: _KeySlot) -> None:
        slot.claim()
        self._slot = slot
        self.ver = ver

    def close(self) -> None:
        self._slot.close()
