"""Encrypted provider connections, OAuth state rows, and sync cursors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from ..memory.crypto import CryptoError
from .crypto import TokenKeyError, TokenKeyring
from .types import ProviderConnection, ProviderCryptoError


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


class ProviderStore:
    def __init__(self, pool: asyncpg.Pool, keyring: TokenKeyring) -> None:
        self._pool = pool
        self._keyring = keyring

    async def get(self, provider: str) -> ProviderConnection | None:
        row = await self._pool.fetchrow(
            "SELECT provider, auth_mode, account_label, account_id, access_enc,"
            " refresh_enc, key_id, token_type, scopes, expires_at, refresh_expires_at,"
            " status, last_refreshed_at, last_error, created_at, updated_at"
            " FROM provider_connections WHERE provider = $1",
            provider,
        )
        if row is None:
            return None
        return self._to_connection(row)

    async def list(self) -> list[ProviderConnection]:
        rows = await self._pool.fetch(
            "SELECT provider, auth_mode, account_label, account_id, access_enc,"
            " refresh_enc, key_id, token_type, scopes, expires_at, refresh_expires_at,"
            " status, last_refreshed_at, last_error, created_at, updated_at"
            " FROM provider_connections ORDER BY provider"
        )
        out: list[ProviderConnection] = []
        for row in rows:
            try:
                out.append(self._to_connection(row))
            except ProviderCryptoError:
                out.append(
                    ProviderConnection(
                        provider=row["provider"],
                        auth_mode=row["auth_mode"],
                        account_label=row["account_label"],
                        account_id=row["account_id"],
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
        access_enc, key_id = self._keyring.encrypt_text(
            connection.access_token, _access_aad(connection.provider)
        )
        refresh_enc = None
        if connection.refresh_token:
            refresh_enc, _ = self._keyring.encrypt_text(
                connection.refresh_token, _refresh_aad(connection.provider)
            )
        await self._pool.execute(
            "INSERT INTO provider_connections"
            " (provider, auth_mode, account_label, account_id, access_enc, refresh_enc,"
            "  key_id, token_type, scopes, expires_at, refresh_expires_at, status,"
            "  last_refreshed_at, last_error, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())"
            " ON CONFLICT (provider) DO UPDATE SET"
            "  auth_mode = EXCLUDED.auth_mode,"
            "  account_label = EXCLUDED.account_label,"
            "  account_id = EXCLUDED.account_id,"
            "  access_enc = EXCLUDED.access_enc,"
            "  refresh_enc = EXCLUDED.refresh_enc,"
            "  key_id = EXCLUDED.key_id,"
            "  token_type = EXCLUDED.token_type,"
            "  scopes = EXCLUDED.scopes,"
            "  expires_at = EXCLUDED.expires_at,"
            "  refresh_expires_at = EXCLUDED.refresh_expires_at,"
            "  status = EXCLUDED.status,"
            "  last_refreshed_at = EXCLUDED.last_refreshed_at,"
            "  last_error = EXCLUDED.last_error,"
            "  updated_at = now()",
            connection.provider,
            connection.auth_mode,
            connection.account_label,
            connection.account_id,
            access_enc,
            refresh_enc,
            key_id,
            connection.token_type,
            connection.scopes,
            connection.expires_at,
            connection.refresh_expires_at,
            connection.status,
            connection.last_refreshed_at,
            connection.last_error,
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
        access_enc, key_id = self._keyring.encrypt_text(access_token, _access_aad(provider))
        refresh_enc = None
        if refresh_token:
            refresh_enc, _ = self._keyring.encrypt_text(refresh_token, _refresh_aad(provider))
        await self._pool.execute(
            "UPDATE provider_connections SET"
            " access_enc = $2, refresh_enc = $3, key_id = $4, scopes = $5,"
            " expires_at = $6, refresh_expires_at = $7, status = $8,"
            " last_refreshed_at = now(), last_error = $9, updated_at = now()"
            " WHERE provider = $1",
            provider,
            access_enc,
            refresh_enc,
            key_id,
            scopes,
            expires_at,
            refresh_expires_at,
            status,
            last_error,
        )
        return key_id

    async def set_status(self, provider: str, status: str, error: str = "") -> None:
        await self._pool.execute(
            "UPDATE provider_connections SET status = $2, last_error = $3,"
            " updated_at = now() WHERE provider = $1",
            provider,
            status,
            error,
        )

    async def delete(self, provider: str) -> None:
        """Remove the connection and everything tied to it. A reconnect
        must start from a clean history cursor, not resume an inbox that
        was never really synced under the new account."""
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
            verifier = self._keyring.decrypt_text(
                bytes(row["code_verifier_enc"]), _state_aad(row["state_hash"]), row["key_id"]
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
        result = await self._pool.execute(
            "DELETE FROM provider_oauth_states WHERE expires_at <= now()"
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    # -- sync cursors --------------------------------------------------------

    async def get_sync(self, provider: str, app: str, key: str) -> str | None:
        return await self._pool.fetchval(
            "SELECT value FROM provider_sync_state WHERE provider = $1 AND app = $2 AND key = $3",
            provider,
            app,
            key,
        )

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        await self._pool.execute(
            "INSERT INTO provider_sync_state (provider, app, key, value, updated_at)"
            " VALUES ($1, $2, $3, $4, now())"
            " ON CONFLICT (provider, app, key) DO UPDATE SET"
            " value = EXCLUDED.value, updated_at = now()",
            provider,
            app,
            key,
            value,
        )

    def _to_connection(self, row: asyncpg.Record) -> ProviderConnection:
        provider = row["provider"]
        try:
            access = self._keyring.decrypt_text(
                bytes(row["access_enc"]), _access_aad(provider), row["key_id"]
            )
            refresh = None
            if row["refresh_enc"] is not None:
                refresh = self._keyring.decrypt_text(
                    bytes(row["refresh_enc"]), _refresh_aad(provider), row["key_id"]
                )
        except (TokenKeyError, CryptoError) as exc:
            raise ProviderCryptoError(f"{provider}'s saved sign-in can't be read") from exc
        return ProviderConnection(
            provider=provider,
            auth_mode=row["auth_mode"],
            account_label=row["account_label"],
            account_id=row["account_id"],
            access_token=access,
            refresh_token=refresh,
            key_id=row["key_id"],
            token_type=row["token_type"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
            status=row["status"],
            last_refreshed_at=row["last_refreshed_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
