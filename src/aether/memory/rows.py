"""Generic row crypto for the storage conversion.

One seam for stores whose rows are sealed by table, column and stable row id.
``CipherRows`` wraps the legacy serving ``Cipher`` and reproduces every AAD and
token purpose exactly. ``ScopedRows`` is the AE2 path for one task's scope;
it ignores the legacy AAD and binds the trusted location instead.

Stores keep their SQL and their semantics. They call ``preflight`` before any
query so a missing grant fails before SQL, and a scoped read raises on a
corrupt body rather than skipping the row.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..vault.scope import KeyScope
from .crypto import Cipher
from .storage import ScopedStore


@runtime_checkable
class Rows(Protocol):
    scoped: bool
    key_version: str

    def preflight(self) -> None: ...

    def seal(
        self,
        value: Any,
        *,
        table: str,
        column: str,
        row_id: Any,
        aad: str | None = None,
    ) -> bytes: ...

    def open(
        self,
        blob: bytes,
        *,
        table: str,
        column: str,
        row_id: Any,
        aad: str | None = None,
    ) -> Any: ...

    def seal_bytes(
        self,
        value: bytes,
        *,
        table: str,
        column: str,
        row_id: Any,
        aad: str | None = None,
    ) -> bytes: ...

    def open_bytes(
        self,
        blob: bytes,
        *,
        table: str,
        column: str,
        row_id: Any,
        aad: str | None = None,
    ) -> bytes: ...

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes: ...

    def dedup(self, domain: str, data: bytes) -> bytes: ...


class CipherRows:
    """The legacy serving path; byte-for-byte the existing Cipher calls."""

    scoped = False

    def __init__(self, cipher: Cipher) -> None:
        self._cipher = cipher

    @property
    def key_version(self) -> str:
        return self._cipher.key_id

    def preflight(self) -> None:
        """The legacy serving key is always present; nothing to check."""

    @staticmethod
    def _aad(table: str, column: str, row_id: Any, aad: str | None) -> str:
        return aad if aad is not None else f"{table}:{column}:{row_id}"

    def seal(
        self, value: Any, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._cipher.encrypt_json(value, aad=self._aad(table, column, row_id, aad))

    def open(
        self, blob: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> Any:
        return self._cipher.decrypt_json(blob, aad=self._aad(table, column, row_id, aad))

    def seal_bytes(
        self, value: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._cipher.encrypt_bytes(value, self._aad(table, column, row_id, aad))

    def open_bytes(
        self, blob: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._cipher.decrypt_bytes(blob, self._aad(table, column, row_id, aad))

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes:
        return self._cipher.blind_token(legacy_purpose, value)

    def dedup(self, domain: str, data: bytes) -> bytes:
        return self._cipher.dedup_mac(data)


class ScopedRows:
    """AE2 for one task's scope; ``key_version`` is the informational ``v<ver>``."""

    scoped = True

    def __init__(self, scope: KeyScope, *, cat: str, ver: int = 1) -> None:
        self._scope = scope
        self._cat = cat
        self._ver = ver
        self._store = ScopedStore(scope, cat=cat, ver=ver)

    @property
    def key_version(self) -> str:
        return f"v{self._ver}"

    def preflight(self) -> None:
        self._scope.require(self._cat, self._ver)

    def seal(
        self, value: Any, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._store.seal(value, table=table, column=column, row_id=row_id)

    def open(
        self, blob: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> Any:
        return self._store.unseal(blob, table=table, column=column, row_id=row_id)

    def seal_bytes(
        self, value: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._store.seal_bytes(value, table=table, column=column, row_id=row_id)

    def open_bytes(
        self, blob: bytes, *, table: str, column: str, row_id: Any, aad: str | None = None
    ) -> bytes:
        return self._store.unseal_bytes(blob, table=table, column=column, row_id=row_id)

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes:
        return self._scope.token(domain, value)

    def dedup(self, domain: str, data: bytes) -> bytes:
        return self._scope.dedup(domain, data)
