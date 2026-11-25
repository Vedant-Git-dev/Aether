"""The scoped key boundary.

A caller acquires a task scope, then seals, opens, tokens or dedups values
without ever holding a raw category key. The scope holds opaque handles and
refuses every location it was not granted. `VaultLocked` is raised before a
store or cache is touched, so a denied read cannot fall back to plaintext.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from os import getpid
from threading import Lock
from typing import Protocol, cast, runtime_checkable

from .errors import VaultProtocolError
from .registry import resolve_dedup, resolve_token
from .schema import (
    AE2_MAGIC,
    CATEGORIES,
    HISTORICAL_LOOKUP_CATEGORIES,
    MAX_SAFE_INTEGER,
    OPERATION_CATEGORIES,
    OPERATIONS,
)

_ROW_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,32}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class VaultError(Exception):
    """Base for a scope or handler refusal."""


class VaultLocked(VaultError):
    """The vault or the task does not hold the requested key."""


class VaultClosed(VaultError):
    """The scope was closed; no further key use is allowed."""


class VaultExhausted(VaultError):
    """The key's encryption budget is spent; rotate before sealing again."""


class LegacyCiphertextError(VaultError):
    """An AE1 or bare ciphertext reached the AE2 path."""


class VaultFormatError(VaultError):
    """A location or blob is malformed for the scoped API."""


class ReservedWrites:
    """An in-memory balance from a committed, non-refundable DB reservation."""

    def __init__(self, vault_id: bytes, cat: str, ver: int, count: int) -> None:
        self._ref = (vault_id, cat, ver)
        self._remaining = count
        self._lock = Lock()
        self._pid = getpid()

    @property
    def ref(self) -> tuple[bytes, str, int]:
        return self._ref

    def spend(self) -> None:
        if getpid() != self._pid:
            raise VaultExhausted("the reservation belongs to a different process")
        with self._lock:
            if self._remaining <= 0:
                raise VaultExhausted("the reserved encryption budget is spent")
            self._remaining -= 1


@dataclass(frozen=True)
class Location:
    """A trusted storage location, supplied by the registry-owning code."""

    cat: str
    ver: int
    table: str
    column: str
    row: str
    purpose: str = "content"

    def __post_init__(self) -> None:
        if self.cat not in CATEGORIES or self.cat == "ingress":
            raise VaultFormatError(f"unknown category {self.cat!r}")
        if type(self.ver) is not int or not 1 <= self.ver <= MAX_SAFE_INTEGER:
            raise VaultFormatError("location version must be a positive safe integer")
        if not _IDENTIFIER_RE.fullmatch(self.table or ""):
            raise VaultFormatError(f"unsafe table {self.table!r}")
        if not _IDENTIFIER_RE.fullmatch(self.column or ""):
            raise VaultFormatError(f"unsafe column {self.column!r}")
        if not isinstance(self.row, str) or not _ROW_RE.fullmatch(self.row):
            raise VaultFormatError(f"unsafe row id {self.row!r}")
        if not isinstance(self.purpose, str) or not _PURPOSE_RE.fullmatch(self.purpose):
            raise VaultFormatError(f"bad purpose {self.purpose!r}")


class KeyResource(Protocol):
    cat: str
    ver: int

    def close(self) -> None: ...


@runtime_checkable
class KeyHandle(KeyResource, Protocol):
    """An opaque storage key. The implementation never exposes key bytes."""

    def seal(self, location: Location, plaintext: bytes) -> bytes: ...

    def open(self, location: Location, blob: bytes) -> bytes: ...

    def token(self, namespace: str, value: bytes) -> bytes: ...

    def dedup(self, namespace: str, value: bytes) -> bytes: ...


@runtime_checkable
class HistoricalLookup(Protocol):
    """A restricted handle for one historical keyed lookup."""

    def tag(self, value: bytes) -> bytes: ...

    def close(self) -> None: ...


def historical_challenge(vault_id: bytes, domain: str) -> bytes:
    return b"aether:historical-lookup-proof:v1\x00" + vault_id + domain.encode("ascii")


@runtime_checkable
class Vault(Protocol):
    """The async context manager a store acquires keys from."""

    def acquire(
        self, task: bytes, operation: str, refs: Iterable[tuple[str, int]]
    ) -> AbstractAsyncContextManager[KeyScope]: ...


class KeyScope:
    """One task's admitted key set. Closing it retires every handle."""

    def __init__(
        self,
        *,
        vault_id: bytes,
        task: bytes,
        operation: str,
        handles: Mapping[tuple[str, int], KeyResource],
        historical: Mapping[str, tuple[HistoricalLookup, ...]] | None = None,
        guard: Callable[[], None] | None = None,
    ) -> None:
        self.vault_id = vault_id
        self.task = task
        self.operation = operation
        self._handles = dict(handles)
        self._historical = dict(historical or {})
        self._guard = guard
        if any(domain not in HISTORICAL_LOOKUP_CATEGORIES for domain in self._historical):
            raise VaultFormatError("unknown historical lookup domain")
        self._closed = False

    @property
    def refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._handles))

    @property
    def closed(self) -> bool:
        return self._closed

    def seal(self, location: Location, plaintext: bytes) -> bytes:
        handle = self._handle(location.cat, location.ver)
        if not isinstance(plaintext, (bytes, bytearray)):
            raise VaultFormatError("seal needs bytes")
        return handle.seal(location, bytes(plaintext))

    def open(self, location: Location, blob: bytes) -> bytes:
        handle = self._handle(location.cat, location.ver)
        if not isinstance(blob, (bytes, bytearray)):
            raise VaultFormatError("open needs bytes")
        if not blob.startswith(AE2_MAGIC):
            if blob.startswith(b"AE1\x00"):
                raise LegacyCiphertextError("AE1 ciphertext is not readable at runtime")
            raise LegacyCiphertextError("ciphertext is not an AE2 envelope")
        return handle.open(location, bytes(blob))

    def token(self, domain: str, value: str | bytes) -> bytes:
        entry = resolve_token(domain)
        handle = self._handle(entry.cat, entry.ver)
        if isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raise VaultFormatError("token needs str or bytes")
        return handle.token(entry.namespace, raw)

    def dedup(self, domain: str, value: bytes) -> bytes:
        entry = resolve_dedup(domain)
        handle = self._handle(entry.cat, entry.ver)
        if not isinstance(value, (bytes, bytearray)):
            raise VaultFormatError("dedup needs bytes")
        return handle.dedup(entry.namespace, bytes(value))

    def require(self, cat: str, ver: int) -> None:
        """Raise unless the scope is open and holds this key."""
        self._lookup(cat, ver)

    def historical_tags(self, domain: str, value: str | bytes) -> tuple[bytes, ...]:
        """Calculate historical tags only while their category is granted."""
        category = HISTORICAL_LOOKUP_CATEGORIES.get(domain)
        if category is None:
            raise VaultFormatError("unknown historical lookup domain")
        if self._closed:
            raise VaultClosed("the key scope is closed")
        if self._guard is not None:
            self._guard()
        if not any(cat == category for cat, _ in self._handles):
            raise VaultLocked(f"{category} is not granted to this task")
        if isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, bytes):
            raw = value
        else:
            raise VaultFormatError("historical lookup needs text or bytes")
        return tuple(handle.tag(raw) for handle in self._historical.get(domain, ()))

    def require_historical(self, domain: str, required: Iterable[bytes]) -> None:
        proofs = self.historical_tags(domain, historical_challenge(self.vault_id, domain))
        if not set(required).issubset(proofs):
            raise VaultLocked("the task is missing required historical lookup keys")

    def close(self) -> None:
        if self._closed:
            return
        # Mark closed first so no use can race the cleanup, then retire every
        # handle even if one handler fails: a live handle is worse than a
        # noisy close.
        self._closed = True
        self._guard = None
        handles = list(self._handles.values())
        handles.extend(handle for group in self._historical.values() for handle in group)
        self._handles.clear()
        self._historical.clear()
        first_error: BaseException | None = None
        for handle in handles:
            try:
                handle.close()
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _renew(
        self, handles: Mapping[tuple[str, int], KeyResource], guard: Callable[[], None]
    ) -> None:
        if self._closed or set(handles) != self._handles.keys():
            raise VaultClosed("the task scope cannot be renewed")
        old = self._handles
        self._handles = dict(handles)
        self._guard = guard
        for handle in old.values():
            handle.close()

    def __enter__(self) -> KeyScope:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _handle(self, cat: str, ver: int) -> KeyHandle:
        if cat == "ingress":
            raise VaultLocked("an ingress key cannot open or seal stored content")
        return cast(KeyHandle, self._lookup(cat, ver))

    def _lookup(self, cat: str, ver: int) -> KeyResource:
        if self._closed:
            raise VaultClosed("the key scope is closed")
        if self._guard is not None:
            self._guard()
        handle = self._handles.get((cat, ver))
        if handle is None:
            raise VaultLocked(f"{cat} v{ver} is not granted to this task")
        return handle


def normalize_refs(refs: Iterable[object]) -> tuple[tuple[str, int], ...]:
    """Accept pairs or `{cat,ver}` objects, then sort and deduplicate-check."""
    out: list[tuple[str, int]] = []
    for raw in refs:
        if isinstance(raw, Mapping):
            cat, ver = raw.get("cat"), raw.get("ver")
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            cat, ver = raw
        else:
            raise VaultProtocolError("bad_field", "a key ref is not a pair")
        if cat not in CATEGORIES:
            raise VaultProtocolError("bad_field", f"unknown category {cat!r}")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise VaultProtocolError("bad_field", "a key ref version must be positive")
        out.append((cat, ver))
    if len(set(out)) != len(out):
        raise VaultProtocolError("bad_field", "key refs must be unique")
    return tuple(sorted(out))


def check_operation(operation: str, refs: Iterable[tuple[str, int]]) -> None:
    if operation not in OPERATIONS:
        raise VaultProtocolError("bad_field", "operation is not in the closed enum")
    categories = {cat for cat, _ in refs}
    if not categories <= OPERATION_CATEGORIES[operation]:
        raise VaultProtocolError("bad_scope", "refs exceed the operation's category set")
