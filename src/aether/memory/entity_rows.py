"""Row crypto for the identity store.

The identity family seals three tables (``identities``, ``identity_handles``,
``entity_notes``) and keeps two keyed lookups (platform and normalized handle).
The legacy serving path wraps its ``Cipher`` here; a scoped task passes
``ScopedEntityRows(scope)`` explicitly. The scoped path fails its preflight
before any SQL and raises on a corrupt body instead of skipping it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..vault.scope import KeyScope
from .crypto import Cipher
from .storage import ScopedStore


@runtime_checkable
class EntityRows(Protocol):
    """Seal, open and tag the identity tables for one task."""

    scoped: bool
    key_version: str

    def preflight(self) -> None: ...

    def seal_profile(self, value: Any, *, identity_id: Any) -> bytes: ...

    def open_profile(self, blob: bytes, *, identity_id: Any) -> Any: ...

    def seal_handle(self, value: Any, *, handle_id: Any) -> bytes: ...

    def open_handle(self, blob: bytes, *, handle_id: Any) -> Any: ...

    def seal_note(self, value: Any, *, note_id: Any) -> bytes: ...

    def open_note(self, blob: bytes, *, note_id: Any) -> Any: ...

    def platform_token(self, platform: str) -> bytes: ...

    def handle_token(self, normalized: str) -> bytes: ...


class CipherEntityRows:
    """The legacy serving path. Reproduces the Cipher calls byte-for-byte."""

    scoped = False

    def __init__(self, cipher: Cipher) -> None:
        self._cipher = cipher

    @property
    def key_version(self) -> str:
        return self._cipher.key_id

    def preflight(self) -> None:
        """The legacy serving key is always present; nothing to check."""

    def seal_profile(self, value: Any, *, identity_id: Any) -> bytes:
        return self._cipher.encrypt_json(value, aad=f"identities:profile_enc:{identity_id}")

    def open_profile(self, blob: bytes, *, identity_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=f"identities:profile_enc:{identity_id}")

    def seal_handle(self, value: Any, *, handle_id: Any) -> bytes:
        return self._cipher.encrypt_json(value, aad=f"identity_handles:handle_enc:{handle_id}")

    def open_handle(self, blob: bytes, *, handle_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=f"identity_handles:handle_enc:{handle_id}")

    def seal_note(self, value: Any, *, note_id: Any) -> bytes:
        return self._cipher.encrypt_json(value, aad=f"entity_notes:payload_enc:{note_id}")

    def open_note(self, blob: bytes, *, note_id: Any) -> Any:
        return self._cipher.decrypt_json(blob, aad=f"entity_notes:payload_enc:{note_id}")

    def platform_token(self, platform: str) -> bytes:
        return self._cipher.blind_token("platform", platform)

    def handle_token(self, normalized: str) -> bytes:
        return self._cipher.blind_token("handle", normalized)


class ScopedEntityRows:
    """AE2 for one task's scope. ``key_version`` is the informational ``v<ver>``."""

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

    def seal_profile(self, value: Any, *, identity_id: Any) -> bytes:
        return self._store.seal(value, table="identities", column="profile_enc", row_id=identity_id)

    def open_profile(self, blob: bytes, *, identity_id: Any) -> Any:
        return self._store.unseal(
            blob, table="identities", column="profile_enc", row_id=identity_id
        )

    def seal_handle(self, value: Any, *, handle_id: Any) -> bytes:
        return self._store.seal(
            value, table="identity_handles", column="handle_enc", row_id=handle_id
        )

    def open_handle(self, blob: bytes, *, handle_id: Any) -> Any:
        return self._store.unseal(
            blob, table="identity_handles", column="handle_enc", row_id=handle_id
        )

    def seal_note(self, value: Any, *, note_id: Any) -> bytes:
        return self._store.seal(value, table="entity_notes", column="payload_enc", row_id=note_id)

    def open_note(self, blob: bytes, *, note_id: Any) -> Any:
        return self._store.unseal(blob, table="entity_notes", column="payload_enc", row_id=note_id)

    def platform_token(self, platform: str) -> bytes:
        return self._scope.token("identity.platform", platform)

    def handle_token(self, normalized: str) -> bytes:
        return self._scope.token("identity.handle", normalized)
