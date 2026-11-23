"""Tests-only key handles and a fake vault for the scoped boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

import reference_wire as wire

from aether.vault.policy import policy
from aether.vault.schema import CATEGORIES
from aether.vault.scope import (
    KeyScope,
    Location,
    ReservedWrites,
    VaultClosed,
    VaultLocked,
    check_operation,
    normalize_refs,
)


class SoftwareKeyHandle:
    """A software stand-in for a native handle. It never exposes its key."""

    def __init__(
        self,
        *,
        vault_id: bytes,
        cat: str,
        ver: int,
        key: bytes,
        writes: ReservedWrites,
    ) -> None:
        self.vault_id = vault_id
        self.cat = cat
        self.ver = ver
        self._key = bytearray(key)
        self._writes = writes
        self._closed = False

    @property
    def zeroed(self) -> bool:
        return self._closed and not any(self._key)

    def _check(self) -> None:
        if self._closed:
            raise VaultClosed("the key handle is closed")

    def _spend(self) -> None:
        self._check()
        self._writes.spend()

    def seal(self, location: Location, plaintext: bytes) -> bytes:
        self._spend()
        return wire.ae2_seal(
            bytes(self._key),
            self.vault_id,
            self.cat,
            self.ver,
            location.table,
            location.column,
            location.row,
            location.purpose,
            plaintext,
        )

    def open(self, location: Location, blob: bytes) -> bytes:
        self._check()
        return wire.ae2_open(
            bytes(self._key),
            self.vault_id,
            self.cat,
            self.ver,
            location.table,
            location.column,
            location.row,
            location.purpose,
            blob,
        )

    def token(self, namespace: str, value: bytes) -> bytes:
        self._check()
        return wire.token_value(
            bytes(self._key), self.vault_id, self.cat, self.ver, namespace, value
        )

    def dedup(self, namespace: str, value: bytes) -> bytes:
        self._check()
        return wire.dedup_value(
            bytes(self._key), self.vault_id, self.cat, self.ver, namespace, value
        )

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._key)):
            self._key[index] = 0
        self._closed = True


class FakeVault:
    """Admits or refuses refs, then hands out software handles."""

    def __init__(
        self,
        *,
        vault_id: bytes,
        keys: dict[tuple[str, int], bytes] | None = None,
        available: bool = True,
        allow: Callable[[bytes, str, tuple[tuple[str, int], ...]], bool] | None = None,
        max_operations: int | None = None,
    ) -> None:
        self.vault_id = vault_id
        self.keys = dict(keys or {})
        if not self.keys:
            self.keys = {(cat, 1): bytes([index + 1]) * 32 for index, cat in enumerate(CATEGORIES)}
        self.available = available
        self.allow = allow
        count = policy().aes_gcm_operational_threshold if max_operations is None else max_operations
        self._budgets = {
            (cat, ver): ReservedWrites(vault_id, cat, ver, count) for cat, ver in self.keys
        }
        self.created_handles: list[SoftwareKeyHandle] = []
        self.acquired = 0
        self.released = 0

    @asynccontextmanager
    async def acquire(self, task: bytes, operation: str, refs: Iterable[Any]) -> Any:
        if not self.available:
            raise VaultLocked("the vault is locked")
        normalized = normalize_refs(refs)
        check_operation(operation, normalized)
        if self.allow is not None and not self.allow(task, operation, normalized):
            raise VaultLocked("the task is not granted these refs")
        if any(ref not in self.keys for ref in normalized):
            raise VaultLocked("one or more requested keys are absent")
        handles: dict[tuple[str, int], SoftwareKeyHandle] = {}
        try:
            for cat, ver in normalized:
                handle = SoftwareKeyHandle(
                    vault_id=self.vault_id,
                    cat=cat,
                    ver=ver,
                    key=self.keys[(cat, ver)],
                    writes=self._budgets[(cat, ver)],
                )
                handles[(cat, ver)] = handle
                self.created_handles.append(handle)
            scope = KeyScope(
                vault_id=self.vault_id, task=task, operation=operation, handles=handles
            )
        except BaseException:
            for handle in handles.values():
                handle.close()
            raise
        self.acquired += 1
        try:
            yield scope
        finally:
            scope.close()
            self.released += 1
