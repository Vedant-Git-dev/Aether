"""The internal event-row crypto interface.

One interface, two implementations. The legacy serving path wraps its
``Cipher`` here at the caller boundary; a scoped task passes
``ScopedEventRows(scope)`` explicitly. Shared helpers (``decode_event_row``)
take this interface, never a ``Cipher | EventRows`` union, and never acquire a
lease themselves: the caller owns the task and its scope.

A scoped row set is bound to one category version and to the task's
``KeyScope``. It reads and writes straight against the database and never
touches the app-wide plaintext index.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..vault.scope import KeyScope
from .crypto import Cipher
from .storage import ScopedStore, historical_lookup_proofs

_TABLE = "events"


@runtime_checkable
class EventRows(Protocol):
    """Seal, open and tag one events row. The table is fixed to ``events``."""

    scoped: bool
    key_version: str

    def preflight(self) -> None: ...

    def seal(self, value: Any, *, column: str, row_id: Any) -> bytes: ...

    def open(self, blob: bytes, *, column: str, row_id: Any) -> Any: ...

    def source_token(self, source: str) -> bytes: ...

    def dedup_tag(self, data: bytes) -> bytes: ...

    def historical_dedup_tags(self, data: bytes) -> tuple[bytes, ...]: ...

    async def verify_historical_dedup(self, connection: Any) -> None: ...


class CipherEventRows:
    """The legacy serving path. Reproduces the Cipher calls byte-for-byte."""

    scoped = False

    def __init__(self, cipher: Cipher) -> None:
        self._cipher = cipher

    @property
    def key_version(self) -> str:
        return self._cipher.key_id

    def preflight(self) -> None:
        """The legacy serving key is always present; nothing to check."""

    def seal(self, value: Any, *, column: str, row_id: Any) -> bytes:
        return self._cipher.encrypt_json(value, aad=f"{_TABLE}:{column}:{row_id}")

    def open(self, blob: bytes, *, column: str, row_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=f"{_TABLE}:{column}:{row_id}")

    def source_token(self, source: str) -> bytes:
        return self._cipher.blind_token("source", source)

    def dedup_tag(self, data: bytes) -> bytes:
        return self._cipher.dedup_mac(data)

    def historical_dedup_tags(self, data: bytes) -> tuple[bytes, ...]:
        return ()

    async def verify_historical_dedup(self, connection: Any) -> None:
        pass


class ScopedEventRows:
    """The AE2 path for one task's scope.

    ``key_version`` is an informational category-version marker (``v<ver>``);
    no reader selects a key from it. Decryption is driven by the scope's
    ``(cat, ver)`` handle, so a row sealed under another version fails closed
    rather than silently picking a different key.
    """

    scoped = True

    def __init__(self, scope: KeyScope, *, cat: str = "memory", ver: int = 1) -> None:
        self._scope = scope
        self._cat = cat
        self._ver = ver
        self._store = ScopedStore(scope, cat=cat, ver=ver)

    @property
    def key_version(self) -> str:
        return f"v{self._ver}"

    def preflight(self) -> None:
        """Fail before any SQL if the scope is closed or lacks this key."""
        self._scope.require(self._cat, self._ver)

    def seal(self, value: Any, *, column: str, row_id: Any) -> bytes:
        return self._store.seal(value, table=_TABLE, column=column, row_id=row_id)

    def open(self, blob: bytes, *, column: str, row_id: Any) -> Any:
        return self._store.unseal(blob, table=_TABLE, column=column, row_id=row_id)

    def source_token(self, source: str) -> bytes:
        return self._scope.token("event.source", source)

    def dedup_tag(self, data: bytes) -> bytes:
        return self._scope.dedup("event.dedup", data)

    def historical_dedup_tags(self, data: bytes) -> tuple[bytes, ...]:
        return self._scope.historical_tags("event.dedup", data)

    async def verify_historical_dedup(self, connection: Any) -> None:
        required = await historical_lookup_proofs(connection, "event.dedup")
        if required:
            self._scope.require_historical("event.dedup", required)
