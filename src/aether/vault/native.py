"""AE2 using libsodium with locked, non-dumpable native key slots on Linux.

The grant installer owns slot initialization. It must decode directly into
the first KEY_BYTES of the slot and then transfer ownership to the handle.
Only public metadata, content, ciphertext and opaque pointers cross this API.
"""

from __future__ import annotations

import ctypes as c
import mmap
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes.util import find_library
from functools import cache
from os import getpid
from secrets import token_bytes
from threading import Lock
from weakref import finalize

from .errors import VaultProtocolError
from .schema import (
    AE2_MAGIC,
    GCM_TAG_BYTES,
    ID_BYTES,
    IV_BYTES,
    KEY_BYTES,
    PROTOCOL_VERSION,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    loads_strict,
)
from .scope import Location, ReservedWrites, VaultClosed, VaultFormatError, VaultLocked

_POINTER = c.c_void_p
_SIZE = c.c_size_t
_LENGTH = c.c_ulonglong


@cache
def _sodium() -> c.CDLL:
    try:
        path = find_library("sodium")
        if not path:
            raise VaultLocked("native vault cryptography is unavailable")
        lib = c.CDLL(path)
        signatures = {
            "sodium_init": (c.c_int,),
            "sodium_mlock": (c.c_int, _POINTER, _SIZE),
            "sodium_memzero": (None, _POINTER, _SIZE),
            "crypto_kdf_hkdf_sha256_extract": (c.c_int, _POINTER, _POINTER, _SIZE, _POINTER, _SIZE),
            "crypto_kdf_hkdf_sha256_expand": (c.c_int, _POINTER, _SIZE, _POINTER, _SIZE, _POINTER),
            "crypto_auth_hmacsha256_statebytes": (_SIZE,),
            "crypto_auth_hmacsha256_init": (c.c_int, _POINTER, _POINTER, _SIZE),
            "crypto_auth_hmacsha256_update": (c.c_int, _POINTER, _POINTER, _LENGTH),
            "crypto_auth_hmacsha256_final": (c.c_int, _POINTER, _POINTER),
            "crypto_aead_aes256gcm_is_available": (c.c_int,),
            "crypto_aead_aes256gcm_statebytes": (_SIZE,),
            "crypto_aead_aes256gcm_beforenm": (c.c_int, _POINTER, _POINTER),
            "crypto_aead_aes256gcm_encrypt_afternm": (
                c.c_int,
                _POINTER,
                _POINTER,
                _POINTER,
                _LENGTH,
                _POINTER,
                _LENGTH,
                _POINTER,
                _POINTER,
                _POINTER,
            ),
            "crypto_aead_aes256gcm_decrypt_afternm": (
                c.c_int,
                _POINTER,
                _POINTER,
                _POINTER,
                _POINTER,
                _LENGTH,
                _POINTER,
                _LENGTH,
                _POINTER,
                _POINTER,
            ),
        }
        for name, (result, *args) in signatures.items():
            function = getattr(lib, name)
            function.argtypes = args
            function.restype = result
        if lib.sodium_init() < 0 or lib.crypto_aead_aes256gcm_is_available() != 1:
            raise VaultLocked("native vault cryptography is unavailable")
        if max(lib.crypto_aead_aes256gcm_statebytes(), lib.crypto_auth_hmacsha256_statebytes()) > (
            mmap.PAGESIZE - 3 * KEY_BYTES
        ):
            raise VaultLocked("native vault crypto state exceeds the protected slot")
    except (OSError, AttributeError) as exc:
        raise VaultLocked("native vault cryptography is unavailable") from exc
    return lib


def _wipe_slot(lib: c.CDLL, page: mmap.mmap, address: int, pid: int) -> None:
    if getpid() == pid:
        lib.sodium_memzero(address, mmap.PAGESIZE)
        page.close()


class _KeySlot:
    """Own the category key, the HKDF PRK and the derived key in one page."""

    def __init__(self) -> None:
        self.lib = _sodium()
        try:
            page = mmap.mmap(-1, mmap.PAGESIZE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            try:
                page.madvise(mmap.MADV_DONTDUMP)
                page.madvise(mmap.MADV_DONTFORK)
                address = c.addressof(c.c_char.from_buffer(page))
                if self.lib.sodium_mlock(address, mmap.PAGESIZE) != 0:
                    raise VaultLocked("could not lock native vault memory")
            except BaseException:
                page.close()
                raise
        except (AttributeError, OSError) as exc:
            raise VaultLocked("could not protect native vault memory") from exc
        self._address = address
        self._pid = getpid()
        self._dispose = finalize(self, _wipe_slot, self.lib, page, address, self._pid)
        self.lock = Lock()
        self._claimed = False

    @property
    def address(self) -> int:
        if not self._dispose.alive or getpid() != self._pid:
            raise VaultClosed("the native key slot is closed")
        return self._address

    def close(self) -> None:
        if self.owned_by_current_process():
            with self.lock:
                self._dispose()

    def owned_by_current_process(self) -> bool:
        return getpid() == self._pid

    def claim(self) -> None:
        if not self.owned_by_current_process():
            raise VaultClosed("the native key slot belongs to a different process")
        with self.lock:
            if self._claimed or not self._dispose.alive:
                raise VaultClosed("the native key slot cannot be transferred")
            self._claimed = True


class NativeKeyHandle:
    """A single-owner handle. A read grant can omit the reserved write balance."""

    def __init__(
        self,
        *,
        vault_id: bytes,
        cat: str,
        ver: int,
        slot: _KeySlot,
        writes: ReservedWrites | None = None,
    ) -> None:
        slot.claim()
        self._slot = slot
        try:
            Location(cat, ver, "vault", "value", "validation")
            if len(vault_id) != ID_BYTES:
                raise VaultFormatError("invalid vault ID")
            if writes is not None and writes.ref != (vault_id, cat, ver):
                raise VaultFormatError("write reservation belongs to a different key")
            self._vault_id = vault_id
            self._header = {
                "v": PROTOCOL_VERSION,
                "vault": b64u_encode(vault_id),
                "cat": cat,
                "ver": ver,
            }
            self.cat = cat
            self.ver = ver
            self._writes = writes
        except BaseException:
            slot.close()
            raise

    def _aad(self, location: Location) -> bytes:
        if (location.cat, location.ver) != (self.cat, self.ver):
            raise VaultLocked("location does not match the native key")
        return canonical_bytes(
            {
                **self._header,
                "table": location.table,
                "column": location.column,
                "row": location.row,
                "purpose": location.purpose,
            }
        )

    @contextmanager
    def _subkey(self, function: str, namespace: str) -> Iterator[tuple[c.CDLL, int, int]]:
        info = canonical_bytes(
            {
                "v": PROTOCOL_VERSION,
                "cat": self.cat,
                "ver": self.ver,
                "fn": function,
                "namespace": namespace,
            }
        )
        if not self._slot.owned_by_current_process():
            raise VaultClosed("the native key slot belongs to a different process")
        with self._slot.lock:
            lib = self._slot.lib
            key = self._slot.address
            prk = key + KEY_BYTES
            subkey = prk + KEY_BYTES
            try:
                if (
                    lib.crypto_kdf_hkdf_sha256_extract(
                        prk, self._vault_id, len(self._vault_id), key, KEY_BYTES
                    )
                    != 0
                    or lib.crypto_kdf_hkdf_sha256_expand(subkey, KEY_BYTES, info, len(info), prk)
                    != 0
                ):
                    raise VaultLocked("native vault key derivation failed")
                yield lib, subkey, subkey + KEY_BYTES
            finally:
                lib.sodium_memzero(prk, mmap.PAGESIZE - KEY_BYTES)

    def seal(self, location: Location, plaintext: bytes) -> bytes:
        aad = self._aad(location)
        with self._subkey("aead", location.purpose) as (lib, subkey, state):
            if self._writes is None:
                raise VaultLocked("this handle has no reserved writes")
            self._writes.spend()
            nonce = token_bytes(IV_BYTES)
            ct = c.create_string_buffer(len(plaintext) + GCM_TAG_BYTES)
            if lib.crypto_aead_aes256gcm_beforenm(state, subkey) != 0 or (
                lib.crypto_aead_aes256gcm_encrypt_afternm(
                    ct, None, plaintext, len(plaintext), aad, len(aad), None, nonce, state
                )
                != 0
            ):
                raise VaultFormatError("AE2 encryption failed")
        return AE2_MAGIC + canonical_bytes(
            {
                **self._header,
                "n": b64u_encode(nonce),
                "ct": b64u_encode(ct.raw),
            }
        )

    def open(self, location: Location, blob: bytes) -> bytes:
        aad = self._aad(location)
        if not blob.startswith(AE2_MAGIC):
            raise VaultFormatError("missing AE2 envelope")
        raw = blob[len(AE2_MAGIC) :]
        envelope = loads_strict(raw)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != self._header.keys() | {"n", "ct"}
            or any(
                type(envelope[k]) is not type(v) or envelope[k] != v
                for k, v in self._header.items()
            )
            or canonical_bytes(envelope) != raw
        ):
            raise VaultFormatError("invalid AE2 header")
        nonce = b64u_decode(envelope["n"], field="n", length=IV_BYTES)
        ct = b64u_decode(envelope["ct"], field="ct")
        size = len(ct) - GCM_TAG_BYTES
        if size < 0:
            raise VaultFormatError("invalid AE2 ciphertext")
        out = c.create_string_buffer(max(1, size))
        with self._subkey("aead", location.purpose) as (lib, subkey, state):
            try:
                if lib.crypto_aead_aes256gcm_beforenm(state, subkey) != 0 or (
                    lib.crypto_aead_aes256gcm_decrypt_afternm(
                        out, None, None, ct, len(ct), aad, len(aad), nonce, state
                    )
                    != 0
                ):
                    raise VaultProtocolError("denied", "AE2 authentication failed")
                return c.string_at(out, size)
            finally:
                lib.sodium_memzero(out, len(out))

    def _tag(self, function: str, namespace: str, value: bytes) -> bytes:
        with self._subkey(function, namespace) as (lib, subkey, state):
            out = c.create_string_buffer(KEY_BYTES)
            if (
                lib.crypto_auth_hmacsha256_init(state, subkey, KEY_BYTES) != 0
                or lib.crypto_auth_hmacsha256_update(state, value, len(value)) != 0
                or lib.crypto_auth_hmacsha256_final(state, out) != 0
            ):
                raise VaultFormatError("native vault tag failed")
            return out.raw

    def token(self, namespace: str, value: bytes) -> bytes:
        return self._tag("token", namespace, value)

    def dedup(self, namespace: str, value: bytes) -> bytes:
        return self._tag("dedup", namespace, value)

    def close(self) -> None:
        self._slot.close()
