"""Encrypted provider connections, OAuth state rows, and sync cursors."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from ..memory.crypto import Cipher, CryptoError
from ..memory.rows import Rows, ScopedRows
from ..vault.scope import KeyScope
from .crypto import TokenKeyError, TokenKeyring
from .types import ProviderConnection, ProviderCryptoError

log = logging.getLogger("aether.providers.store")


@dataclass(frozen=True)
class OAuthState:
    state_hash: str
    provider: str
    app: str
    scopes: str
    code_verifier: str
    redirect_uri: str


def _access_aad(provider: str) -> str:
    return f"provider_connections:access_enc:{provider}"


def _refresh_aad(provider: str) -> str:
    return f"provider_connections:refresh_enc:{provider}"


def _state_aad(state_hash: str) -> str:
    return f"provider_oauth_states:code_verifier_enc:{state_hash}"


def _account_aad(provider: str, column: str) -> str:
    return f"provider_connections:{column}:{provider}"


def _sync_aad(provider: str, app: str, key: str) -> str:
    return f"provider_sync_state:value_enc:{provider}\x1f{app}\x1f{key}"


def _sync_row_id(provider: str, app: str, key: str) -> str:
    """A safe, stable location row for the composite sync key: the scoped
    location grammar allows only [A-Za-z0-9_.:-], so hash the raw join."""
    return hashlib.sha256(f"{provider}\x1f{app}\x1f{key}".encode()).hexdigest()


class ProviderStore:
    """Provider connections, OAuth states and sync cursors.

    The legacy serving path seals tokens under a separate `TokenKeyring`
    family and everything else under the content cipher. The scoped path
    seals every field through one task's `KeyScope`; the `key_id` column
    stays as an informational label and no longer selects a key.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        keyring: TokenKeyring | None = None,
        cipher: Cipher | None = None,
        *,
        rows: Rows | None = None,
    ) -> None:
        if rows is None and keyring is None:
            raise ValueError("ProviderStore needs a keyring (legacy) or rows (scoped)")
        if rows is not None and keyring is not None:
            raise ValueError("ProviderStore takes a keyring or rows, not both")
        self._pool = pool
        self._keyring = keyring
        self._cipher = cipher
        self._rows: Rows | None = rows
        self._scoped = rows is not None and rows.scoped

    @classmethod
    def scoped(cls, pool: asyncpg.Pool, scope: KeyScope, *, ver: int = 1) -> ProviderStore:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, cipher=None, rows=ScopedRows(scope, cat="credentials", ver=ver))

    # -- field crypto -------------------------------------------------------

    def _key_label(self) -> str:
        if self._scoped:
            assert self._rows is not None
            return self._rows.key_version
        return self._cipher.key_id if self._cipher is not None else "k1"

    def _seal_token(self, value: str, provider: str, column: str) -> tuple[bytes, str]:
        if self._scoped:
            assert self._rows is not None
            blob = self._rows.seal_bytes(
                value.encode("utf-8"),
                table="provider_connections",
                column=column,
                row_id=provider,
            )
            return blob, self._rows.key_version
        assert self._keyring is not None
        prefix = _access_aad(provider) if column == "access_enc" else _refresh_aad(provider)
        return self._keyring.encrypt_text(value, prefix)

    def _open_token(self, provider: str, blob: Any, column: str, key_id: str) -> str:
        if self._scoped:
            assert self._rows is not None
            return self._rows.open_bytes(
                bytes(blob),
                table="provider_connections",
                column=column,
                row_id=provider,
            ).decode("utf-8")
        assert self._keyring is not None
        prefix = _access_aad(provider) if column == "access_enc" else _refresh_aad(provider)
        return self._keyring.decrypt_text(bytes(blob), prefix, key_id)

    def _seal_account(self, value: str, provider: str, column: str) -> bytes | None:
        if self._scoped:
            assert self._rows is not None
            return self._rows.seal_bytes(
                value.encode("utf-8"),
                table="provider_connections",
                column=column,
                row_id=provider,
            )
        if self._cipher is None:
            return None
        return self._cipher.encrypt_text(value, aad=_account_aad(provider, column))

    def _open_field(self, blob: Any, provider: str, column: str) -> str:
        if not blob:
            return ""
        try:
            if self._scoped:
                assert self._rows is not None
                return self._rows.open_bytes(
                    bytes(blob),
                    table="provider_connections",
                    column=column,
                    row_id=provider,
                ).decode("utf-8")
            if self._cipher is None:
                return ""
            return self._cipher.decrypt_text(bytes(blob), aad=_account_aad(provider, column))
        except CryptoError:
            if self._scoped:
                raise
            log.warning("provider field undecryptable — treated as empty")
            return ""

    def _preflight(self) -> None:
        if self._scoped:
            assert self._rows is not None
            self._rows.preflight()

    # -- connections --------------------------------------------------------

    async def get(self, provider: str) -> ProviderConnection | None:
        self._preflight()
        row = await self._pool.fetchrow(
            "SELECT provider, auth_mode, access_enc, refresh_enc, key_id,"
            " token_type, scopes, expires_at, refresh_expires_at, status,"
            " last_refreshed_at, account_label_enc, account_id_enc,"
            " last_error_enc, created_at, updated_at"
            " FROM provider_connections WHERE provider = $1",
            provider,
        )
        if row is None:
            return None
        return self._to_connection(row)

    @staticmethod
    async def refresh_due(pool: asyncpg.Pool) -> list[str]:
        rows = await pool.fetch(
            "SELECT provider FROM provider_connections"
            " WHERE auth_mode = 'oauth' AND status = 'active' AND expires_at <= now()"
            " ORDER BY provider"
        )
        return [row["provider"] for row in rows]

    async def claim_refresh(self, provider: str) -> bool:
        self._preflight()
        row = await self._pool.fetchrow(
            "UPDATE provider_connections SET status = 'refreshing', updated_at = now()"
            " WHERE provider = $1 AND auth_mode = 'oauth' AND status = 'active'"
            " AND expires_at <= now() RETURNING provider",
            provider,
        )
        return row is not None

    async def list(self) -> list[ProviderConnection]:
        self._preflight()
        rows = await self._pool.fetch(
            "SELECT provider, auth_mode, access_enc, refresh_enc, key_id,"
            " token_type, scopes, expires_at, refresh_expires_at, status,"
            " last_refreshed_at, account_label_enc, account_id_enc,"
            " last_error_enc, created_at, updated_at"
            " FROM provider_connections ORDER BY provider"
        )
        out: list[ProviderConnection] = []
        for row in rows:
            try:
                out.append(self._to_connection(row))
            except ProviderCryptoError:
                if self._scoped:
                    raise
                out.append(
                    ProviderConnection(
                        provider=row["provider"],
                        auth_mode=row["auth_mode"],
                        account_label=self._open_field(
                            row.get("account_label_enc"), row["provider"], "account_label_enc"
                        ),
                        account_id=self._open_field(
                            row.get("account_id_enc"), row["provider"], "account_id_enc"
                        ),
                        key_id=row["key_id"],
                        token_type=row["token_type"],
                        scopes=row["scopes"],
                        expires_at=row["expires_at"],
                        refresh_expires_at=row["refresh_expires_at"],
                        status="reauth_required",
                        last_refreshed_at=row["last_refreshed_at"],
                        last_error="the saved sign-in can't be read",
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
        return out

    async def upsert(self, connection: ProviderConnection) -> ProviderConnection:
        self._preflight()
        access_enc, key_id = self._seal_token(
            connection.access_token, connection.provider, "access_enc"
        )
        refresh_enc = None
        if connection.refresh_token:
            refresh_enc, _ = self._seal_token(
                connection.refresh_token, connection.provider, "refresh_enc"
            )
        await self._pool.execute(
            "INSERT INTO provider_connections"
            " (provider, auth_mode, access_enc, refresh_enc, key_id, token_type,"
            "  scopes, expires_at, refresh_expires_at, status, last_refreshed_at,"
            "  updated_at, account_label_enc, account_id_enc, last_error_enc,"
            "  key_version)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(),"
            "  $12, $13, $14, $15)"
            " ON CONFLICT (provider) DO UPDATE SET"
            "  auth_mode = EXCLUDED.auth_mode,"
            "  access_enc = EXCLUDED.access_enc,"
            "  refresh_enc = EXCLUDED.refresh_enc,"
            "  key_id = EXCLUDED.key_id,"
            "  token_type = EXCLUDED.token_type,"
            "  scopes = EXCLUDED.scopes,"
            "  expires_at = EXCLUDED.expires_at,"
            "  refresh_expires_at = EXCLUDED.refresh_expires_at,"
            "  status = EXCLUDED.status,"
            "  last_refreshed_at = EXCLUDED.last_refreshed_at,"
            "  account_label_enc = EXCLUDED.account_label_enc,"
            "  account_id_enc = EXCLUDED.account_id_enc,"
            "  last_error_enc = EXCLUDED.last_error_enc,"
            "  key_version = EXCLUDED.key_version,"
            "  updated_at = now()",
            connection.provider,
            connection.auth_mode,
            access_enc,
            refresh_enc,
            key_id,
            connection.token_type,
            connection.scopes,
            connection.expires_at,
            connection.refresh_expires_at,
            connection.status,
            connection.last_refreshed_at,
            self._seal_account(connection.account_label, connection.provider, "account_label_enc"),
            self._seal_account(connection.account_id, connection.provider, "account_id_enc"),
            self._seal_account(connection.last_error, connection.provider, "last_error_enc"),
            self._key_label(),
        )
        connection.key_id = key_id
        return connection

    async def update_tokens(
        self,
        provider: str,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime | None,
        refresh_expires_at: datetime | None,
        scopes: str,
        status: str = "active",
        last_error: str = "",
    ) -> str:
        self._preflight()
        access_enc, key_id = self._seal_token(access_token, provider, "access_enc")
        refresh_enc = None
        if refresh_token:
            refresh_enc, _ = self._seal_token(refresh_token, provider, "refresh_enc")
        await self._pool.execute(
            "UPDATE provider_connections SET"
            " access_enc = $2, refresh_enc = $3, key_id = $4, scopes = $5,"
            " expires_at = $6, refresh_expires_at = $7, status = $8,"
            " last_refreshed_at = now(), last_error_enc = $9,"
            " key_version = $10, updated_at = now()"
            " WHERE provider = $1",
            provider,
            access_enc,
            refresh_enc,
            key_id,
            scopes,
            expires_at,
            refresh_expires_at,
            status,
            self._seal_account(last_error, provider, "last_error_enc"),
            self._key_label(),
        )
        return key_id

    async def set_status(self, provider: str, status: str, error: str = "") -> None:
        self._preflight()
        await self._pool.execute(
            "UPDATE provider_connections SET status = $2, last_error_enc = $3,"
            " key_version = $4, updated_at = now()"
            " WHERE provider = $1",
            provider,
            status,
            self._seal_account(error, provider, "last_error_enc"),
            self._key_label(),
        )

    async def delete(self, provider: str) -> None:
        """Remove the connection and everything tied to it. A reconnect
        must start from a clean history cursor, not resume an inbox that
        was never really synced under the new account."""
        self._preflight()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM provider_connections WHERE provider = $1", provider)
            await conn.execute("DELETE FROM provider_sync_state WHERE provider = $1", provider)
            await conn.execute("DELETE FROM provider_oauth_states WHERE provider = $1", provider)

    # -- OAuth state ---------------------------------------------------------

    async def create_state(
        self,
        *,
        state_hash: str,
        provider: str,
        app: str,
        scopes: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None:
        self._preflight()
        if self._scoped:
            assert self._rows is not None
            blob = self._rows.seal_bytes(
                code_verifier.encode("utf-8"),
                table="provider_oauth_states",
                column="code_verifier_enc",
                row_id=state_hash,
            )
            key_id = self._rows.key_version
        else:
            assert self._keyring is not None
            blob, key_id = self._keyring.encrypt_text(code_verifier, _state_aad(state_hash))
        await self._pool.execute(
            "INSERT INTO provider_oauth_states"
            " (state_hash, provider, app, scopes, code_verifier_enc, key_id,"
            "  redirect_uri, expires_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            state_hash,
            provider,
            app,
            scopes,
            blob,
            key_id,
            redirect_uri,
            expires_at,
        )

    async def consume_state(self, state_hash: str) -> OAuthState | None:
        self._preflight()
        row = await self._pool.fetchrow(
            "UPDATE provider_oauth_states SET consumed_at = now()"
            " WHERE state_hash = $1 AND consumed_at IS NULL AND expires_at > now()"
            " RETURNING state_hash, provider, app, scopes, code_verifier_enc,"
            " key_id, redirect_uri",
            state_hash,
        )
        if row is None:
            return None
        try:
            if self._scoped:
                assert self._rows is not None
                verifier = self._rows.open_bytes(
                    bytes(row["code_verifier_enc"]),
                    table="provider_oauth_states",
                    column="code_verifier_enc",
                    row_id=row["state_hash"],
                ).decode("utf-8")
            else:
                assert self._keyring is not None
                verifier = self._keyring.decrypt_text(
                    bytes(row["code_verifier_enc"]),
                    _state_aad(row["state_hash"]),
                    row["key_id"],
                )
        except (TokenKeyError, CryptoError) as exc:
            raise ProviderCryptoError("the sign-in state can't be read") from exc
        return OAuthState(
            state_hash=row["state_hash"],
            provider=row["provider"],
            app=row["app"],
            scopes=row["scopes"],
            code_verifier=verifier,
            redirect_uri=row["redirect_uri"],
        )

    async def delete_expired_states(self) -> int:
        self._preflight()
        result = await self._pool.execute(
            "DELETE FROM provider_oauth_states WHERE expires_at <= now()"
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    # -- sync cursors --------------------------------------------------------

    async def get_sync(self, provider: str, app: str, key: str) -> str | None:
        self._preflight()
        row = await self._pool.fetchrow(
            "SELECT value_enc FROM provider_sync_state"
            " WHERE provider = $1 AND app = $2 AND key = $3",
            provider,
            app,
            key,
        )
        if row is None or row["value_enc"] is None:
            return None
        try:
            if self._scoped:
                assert self._rows is not None
                return self._rows.open_bytes(
                    bytes(row["value_enc"]),
                    table="provider_sync_state",
                    column="value_enc",
                    row_id=_sync_row_id(provider, app, key),
                ).decode("utf-8")
            if self._cipher is None:
                return None
            return self._cipher.decrypt_text(row["value_enc"], aad=_sync_aad(provider, app, key))
        except CryptoError:
            if self._scoped:
                raise
            log.warning("sync cursor for %s/%s/%s is undecryptable", provider, app, key)
            return None

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        self._preflight()
        if self._scoped:
            assert self._rows is not None
            blob = self._rows.seal_bytes(
                value.encode("utf-8"),
                table="provider_sync_state",
                column="value_enc",
                row_id=_sync_row_id(provider, app, key),
            )
        else:
            if self._cipher is None:
                raise RuntimeError(
                    "the content cipher is not configured — cannot seal a sync cursor"
                )
            blob = self._cipher.encrypt_text(value, aad=_sync_aad(provider, app, key))
        await self._pool.execute(
            "INSERT INTO provider_sync_state"
            " (provider, app, key, value_enc, key_version, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, now())"
            " ON CONFLICT (provider, app, key) DO UPDATE SET"
            " value_enc = EXCLUDED.value_enc,"
            " key_version = EXCLUDED.key_version, updated_at = now()",
            provider,
            app,
            key,
            blob,
            self._key_label(),
        )

    def _to_connection(self, row: asyncpg.Record) -> ProviderConnection:
        provider = row["provider"]
        try:
            access = self._open_token(provider, row["access_enc"], "access_enc", row["key_id"])
            refresh = None
            if row["refresh_enc"] is not None:
                refresh = self._open_token(
                    provider, row["refresh_enc"], "refresh_enc", row["key_id"]
                )
        except (TokenKeyError, CryptoError) as exc:
            raise ProviderCryptoError(f"{provider}'s saved sign-in can't be read") from exc
        return ProviderConnection(
            provider=provider,
            auth_mode=row["auth_mode"],
            account_label=self._open_field(
                row.get("account_label_enc"), provider, "account_label_enc"
            ),
            account_id=self._open_field(row.get("account_id_enc"), provider, "account_id_enc"),
            access_token=access,
            refresh_token=refresh,
            key_id=row["key_id"],
            token_type=row["token_type"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
            status=row["status"],
            last_refreshed_at=row["last_refreshed_at"],
            last_error=self._open_field(row.get("last_error_enc"), provider, "last_error_enc"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
