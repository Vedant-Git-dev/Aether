"""Row crypto for stores that seal one body column.

The same boundary as the event rows: the legacy serving path wraps its
``Cipher``, while a scoped task passes a ``ScopedBodyRows(scope)`` explicitly.
A scoped row set fails its preflight before any SQL and fails closed on a
corrupt or wrong-version body instead of skipping the row.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..vault.scope import KeyScope
from .crypto import Cipher
from .storage import ScopedStore


@runtime_checkable
class BodyRows(Protocol):
    """Seal, open and tag one body column. The table is fixed per store."""

    scoped: bool
    key_version: str

    def preflight(self) -> None: ...

    def seal(self, value: Any, *, row_id: Any) -> bytes: ...

    def open(self, blob: bytes, *, row_id: Any) -> Any: ...

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes: ...


class CipherBodyRows:
    """The legacy serving path. Reproduces the Cipher calls byte-for-byte."""

    scoped = False

    def __init__(self, cipher: Cipher, *, table: str, column: str = "body_enc") -> None:
        self._cipher = cipher
        self._table = table
        self._column = column

    @property
    def key_version(self) -> str:
        return self._cipher.key_id

    def preflight(self) -> None:
        """The legacy serving key is always present; nothing to check."""

    def _aad(self, row_id: Any) -> str:
        return f"{self._table}:{self._column}:{row_id}"

    def seal(self, value: Any, *, row_id: Any) -> bytes:
        return self._cipher.encrypt_json(value, aad=self._aad(row_id))

    def open(self, blob: bytes, *, row_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=self._aad(row_id))

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes:
        return self._cipher.blind_token(legacy_purpose, value)


class ScopedBodyRows:
    """AE2 for one task's scope. ``key_version`` is the informational ``v<ver>``."""

    scoped = True

    def __init__(
        self,
        scope: KeyScope,
        *,
        table: str,
        cat: str = "memory",
        ver: int = 1,
        column: str = "body_enc",
    ) -> None:
        self._scope = scope
        self._cat = cat
        self._ver = ver
        self._table = table
        self._column = column
        self._store = ScopedStore(scope, cat=cat, ver=ver)

    @property
    def key_version(self) -> str:
        return f"v{self._ver}"

    def preflight(self) -> None:
        """Fail before any SQL if the scope is closed or lacks this key."""
        self._scope.require(self._cat, self._ver)

    def seal(self, value: Any, *, row_id: Any) -> bytes:
        return self._store.seal(value, table=self._table, column=self._column, row_id=row_id)

    def open(self, blob: bytes, *, row_id: Any) -> Any:
        return self._store.unseal(blob, table=self._table, column=self._column, row_id=row_id)

    def token(self, domain: str, legacy_purpose: str, value: str | bytes) -> bytes:
        return self._scope.token(domain, value)
