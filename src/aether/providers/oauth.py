"""The one OAuth 2.0 engine every provider uses."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..authz.audit import AuditLog
from ..secret_env import EnvResolver
from .store import ProviderStore
from .types import (
    AppDefinition,
    ProviderAuthError,
    ProviderConnection,
    ProviderDefinition,
    ProviderHTTPError,
    ProviderNotConfigured,
    ProviderReauthRequired,
)

_STATE_TTL = timedelta(minutes=10)
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


class OAuthEngine:
    def __init__(
        self,
        *,
        store: ProviderStore,
        resolver: EnvResolver,
        http: httpx.AsyncClient,
        audit: AuditLog,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._http = http
        self._audit = audit

    async def client(self, provider: ProviderDefinition) -> tuple[str, str] | None:
        if not provider.client_id_name or not provider.client_secret_name:
            return None
        client_id = await self._resolver.resolve(provider.client_id_name)
        client_secret = await self._resolver.resolve(provider.client_secret_name)
        if not client_id or not client_secret:
            return None
        return client_id, client_secret

    async def client_configured(self, provider: ProviderDefinition) -> bool:
        return await self.client(provider) is not None

    async def authorization_url(
        self,
        provider: ProviderDefinition,
        app: AppDefinition,
        *,
        redirect_uri: str,
        existing_scopes: tuple[str, ...] = (),
        prompt_consent: bool = False,
    ) -> str:
        if not provider.authorization_endpoint:
            raise ProviderNotConfigured(f"{provider.name} has no authorization endpoint")
        client = await self.client(provider)
        if client is None:
            raise ProviderNotConfigured(f"{provider.name} isn't set up yet")
        client_id, _ = client

        scopes = tuple(dict.fromkeys((*existing_scopes, *app.scopes, *provider.base_scopes)))
        state = secrets.token_urlsafe(32)
        state_hash = _state_hash(state)
        verifier = ""
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }
        if provider.pkce:
            verifier, challenge = _pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
        params.update(provider.extra_auth_params)
        if prompt_consent:
            params["prompt"] = "consent"

        await self._store.create_state(
            state_hash=state_hash,
            provider=provider.key,
            app=app.key,
            scopes=" ".join(scopes),
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + _STATE_TTL,
        )
        return f"{provider.authorization_endpoint}?{urlencode(params)}"

    async def exchange(
        self,
        provider: ProviderDefinition,
        *,
        code: str,
        state: str,
    ) -> ProviderConnection:
        state_row = await self._store.consume_state(_state_hash(state))
        if state_row is None:
            raise ProviderAuthError("the sign-in link expired or was already used")
        if state_row.provider != provider.key:
            raise ProviderAuthError("that sign-in belongs to a different app")
        client = await self.client(provider)
        if client is None:
            raise ProviderNotConfigured(f"{provider.name} isn't set up yet")
        client_id, client_secret = client
        if not provider.token_endpoint:
            raise ProviderNotConfigured(f"{provider.name} has no token endpoint")

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": state_row.redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if provider.pkce:
            form["code_verifier"] = state_row.code_verifier
        payload = await self._token_post(provider.token_endpoint, form)
        access = str(payload.get("access_token") or "")
        if not access:
            raise ProviderAuthError("the provider didn't return an access token")
        refresh = str(payload.get("refresh_token") or "") or None
        expires_at = _expiry(payload.get("expires_in"))
        token_type = str(payload.get("token_type") or "Bearer")
        scopes = str(payload.get("scope") or state_row.scopes)
        account_id, label = await self.identity(provider, access)
        connection = ProviderConnection(
            provider=provider.key,
            auth_mode="oauth",
            account_label=label,
            account_id=account_id,
            app=state_row.app,
            access_token=access,
            refresh_token=refresh,
            token_type=token_type,
            scopes=scopes,
            expires_at=expires_at,
            status="active",
            last_refreshed_at=datetime.now(UTC),
        )
        await self._store.upsert(connection)
        await self._audit.append(
            actor="owner",
            tool_name=f"{state_row.app}__connect",
            decision="allow",
            rules_matched="builtin:oauth-consent",
            params={"provider": provider.key, "app": state_row.app, "scopes": scopes},
            outcome=f"{provider.name} connected",
        )
        return connection

    async def refresh(
        self, provider: ProviderDefinition, connection: ProviderConnection
    ) -> ProviderConnection:
        if connection.auth_mode != "oauth" or not connection.refresh_token:
            raise ProviderReauthRequired(f"{provider.name} needs to be reconnected")
        client = await self.client(provider)
        if client is None:
            raise ProviderNotConfigured(f"{provider.name} isn't set up yet")
        client_id, client_secret = client
        if not provider.token_endpoint:
            raise ProviderNotConfigured(f"{provider.name} has no token endpoint")
        payload = await self._token_post(
            provider.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        access = str(payload.get("access_token") or "")
        if not access:
            raise ProviderReauthRequired(f"{provider.name} rejected the refresh")
        refresh = str(payload.get("refresh_token") or "") or connection.refresh_token
        scopes = str(payload.get("scope") or connection.scopes)
        expires_at = _expiry(payload.get("expires_in"))
        key_id = await self._store.update_tokens(
            provider.key,
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            refresh_expires_at=connection.refresh_expires_at,
            scopes=scopes,
            status="active",
        )
        connection.access_token = access
        connection.refresh_token = refresh
        connection.scopes = scopes
        connection.expires_at = expires_at
        connection.key_id = key_id
        connection.status = "active"
        connection.last_error = ""
        connection.last_refreshed_at = datetime.now(UTC)
        return connection

    async def revoke(self, provider: ProviderDefinition, token: str) -> None:
        if not provider.revocation_endpoint or not token:
            return
        try:
            await self._http.post(
                provider.revocation_endpoint,
                data={"token": token},
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError:
            # local deletion is the promise; a remote revoke is best effort
            return

    async def identity(self, provider: ProviderDefinition, access_token: str) -> tuple[str, str]:
        if not provider.userinfo_endpoint:
            return "", provider.name
        try:
            response = await self._http.get(
                provider.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(502, f"{provider.name} isn't responding right now") from exc
        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code,
                f"{provider.name} didn't accept the sign-in",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAuthError(f"{provider.name} returned an unreadable account") from exc
        if not isinstance(data, dict):
            return "", provider.name
        account_id = str(
            data.get("sub") or data.get("id") or data.get("login") or data.get("email") or ""
        )
        label = str(
            data.get("email")
            or data.get("login")
            or data.get("preferred_username")
            or data.get("name")
            or provider.name
        )
        return account_id, label

    async def _token_post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                url,
                data=form,
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(502, "the provider isn't responding right now") from exc
        if response.status_code >= 400:
            raise ProviderAuthError("the provider rejected the sign-in")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderAuthError("the provider returned an unreadable sign-in") from exc
        if not isinstance(payload, dict):
            raise ProviderAuthError("the provider returned an unreadable sign-in")
        if payload.get("error"):
            raise ProviderAuthError("the provider rejected the sign-in")
        return payload


def _expiry(seconds: Any) -> datetime | None:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=value)
