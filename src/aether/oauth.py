"""OAuth from chat: consent links, the tamper-proof state that rides them,
and the encrypted home for what a provider hands back.

An /apps walk sends the consent link in chat; the user's browser round-trip
lands on the public `GET /oauth/callback`, which verifies the state,
exchanges the code, stores the tokens encrypted (same AES-256-GCM as all
content — AAD `"oauth_tokens:<column>:<provider>"`, the config_overrides
shape), and tells the loop to finish the connection. Tokens are the one
class of secret that never passes through chat at all: they arrive
provider→callback and never touch a paste. Keys and the BYOA client
credentials do paste — consumed before ingest into the encrypted store
(the secret_env/secret_store story) — and a consent link built here asks
for the union of stored and requested scopes, so a second Google app
re-signs without breaking the first.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from .memory.crypto import Cipher, CryptoError

log = logging.getLogger("aether.oauth")

PostFn = Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]]

# a state is good for ten minutes — long enough for a human to read a
# consent screen, short enough that a leaked link is worthless
_STATE_TTL = 600.0


class OAuthError(Exception):
    """Misconfigured OAuth (no key, bad code exchange) — surfaced in plain
    words in chat, never a traceback."""


@dataclass(frozen=True)
class OAuthProvider:
    key: str
    label: str
    auth_url: str
    token_url: str


PROVIDERS: dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        key="google",
        label="Google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
    )
}


def callback_url(public_url: str) -> str:
    """Where the browser lands after consent — the address the user must
    have registered on the provider's console for this exact deployment."""
    return public_url.rstrip("/") + "/oauth/callback"


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    scopes: str
    expires_at: datetime


class OAuthFlow:
    """Consent links and the state that makes the callback self-verifying.

    The state is `provider|app|redirect|expiry` + HMAC-SHA256 with the
    encryption key: tamper-proof, expiring, and carrying the redirect the
    consent link used (so the code exchange replays the exact same URI —
    and a walk that collected the public address in chat still works after
    the walk is gone). AETHER_TOKEN never appears in any URL.
    """

    def __init__(self, key_b64: str) -> None:
        if not key_b64:
            raise OAuthError(
                "google sign-in needs AETHER_ENCRYPTION_KEY set — the tokens "
                "are stored encrypted, so a permanent key must exist first"
            )
        try:
            self._key = base64.b64decode(key_b64.strip(), validate=True)
        except Exception as exc:
            raise OAuthError(f"invalid base64 encryption key: {exc}") from exc
        if len(self._key) != 32:
            raise OAuthError(f"encryption key must decode to 32 bytes, got {len(self._key)}")

    def consent_url(
        self,
        *,
        provider: str,
        app: str,
        client_id: str,
        redirect: str,
        scopes: str,
        now: float | None = None,
    ) -> str:
        prov = PROVIDERS[provider]
        now = time.time() if now is None else now
        state = self._sign(provider, app, redirect, now + _STATE_TTL)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": scopes,
            "state": state,
            "access_type": "offline",  # a refresh token, so sign-in is once
            "prompt": "consent",
        }
        return f"{prov.auth_url}?{urlencode(params)}"

    def verify_state(self, state: str, now: float | None = None) -> tuple[str, str, str] | None:
        """(provider, app, redirect) when the state is genuine and unexpired."""
        try:
            text = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
        except Exception:
            return None
        payload, sep, sig = text.rpartition("|")
        if not sep or not payload:
            return None
        expected = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        parts = payload.split("|")
        if len(parts) != 4:
            return None
        provider, app, redirect, exp = parts
        if provider not in PROVIDERS:
            return None
        try:
            if float(exp) < (time.time() if now is None else now):
                return None
        except ValueError:
            return None
        return provider, app, redirect

    def _sign(self, provider: str, app: str, redirect: str, exp: float) -> str:
        payload = f"{provider}|{app}|{redirect}|{int(exp)}"
        sig = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode("ascii")


async def _http_post(url: str, data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, data=data)
        response.raise_for_status()
        return dict(response.json())


def _token_set(body: dict[str, Any], *, refresh_token: str, now: float) -> TokenSet:
    expires_in = float(body.get("expires_in") or 3600)
    return TokenSet(
        access_token=str(body["access_token"]),
        refresh_token=refresh_token or str(body.get("refresh_token") or ""),
        scopes=str(body.get("scope") or ""),
        expires_at=datetime.fromtimestamp(now + expires_in, UTC),
    )


async def exchange_code(
    provider: str,
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect: str,
    post: PostFn = _http_post,
    now: float | None = None,
) -> TokenSet:
    """The authorization code for tokens, at the provider's token endpoint."""
    now = time.time() if now is None else now
    body = await post(
        PROVIDERS[provider].token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
        },
    )
    _raise_for_error(body)
    return _token_set(body, refresh_token="", now=now)


async def refresh_access(
    provider: str,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    post: PostFn = _http_post,
    now: float | None = None,
) -> TokenSet:
    """A fresh access token from the stored refresh one — the background
    refresher's one move."""
    now = time.time() if now is None else now
    body = await post(
        PROVIDERS[provider].token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    _raise_for_error(body)
    return _token_set(body, refresh_token=refresh_token, now=now)


def _raise_for_error(body: dict[str, Any]) -> None:
    if isinstance(body, dict) and body.get("error"):
        detail = body.get("error_description") or ""
        raise OAuthError(f"the provider refused: {body['error']}" + (f" — {detail}" if detail else ""))


class OAuthTokenStore:
    """Provider tokens in Postgres, encrypted at rest — the ConfigOverrides
    shape (one-step upsert, path-bound AAD), provider-keyed."""

    def __init__(self, pool: Any, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def save(self, provider: str, tokens: TokenSet) -> None:
        await self._pool.execute(
            "INSERT INTO oauth_tokens (provider, access_enc, refresh_enc, scopes, expires_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, now())"
            " ON CONFLICT (provider) DO UPDATE SET"
            " access_enc = EXCLUDED.access_enc, refresh_enc = EXCLUDED.refresh_enc,"
            " scopes = EXCLUDED.scopes, expires_at = EXCLUDED.expires_at, updated_at = now()",
            provider,
            self._cipher.encrypt_text(tokens.access_token, f"oauth_tokens:access_enc:{provider}"),
            self._cipher.encrypt_text(tokens.refresh_token, f"oauth_tokens:refresh_enc:{provider}"),
            tokens.scopes,
            tokens.expires_at,
        )

    async def update_access(self, provider: str, access_token: str, expires_at: datetime) -> None:
        await self._pool.execute(
            "UPDATE oauth_tokens SET access_enc = $2, expires_at = $3, updated_at = now()"
            " WHERE provider = $1",
            provider,
            self._cipher.encrypt_text(access_token, f"oauth_tokens:access_enc:{provider}"),
            expires_at,
        )

    async def get(self, provider: str) -> TokenSet | None:
        row = await self._pool.fetchrow(
            "SELECT access_enc, refresh_enc, scopes, expires_at FROM oauth_tokens WHERE provider = $1",
            provider,
        )
        if row is None:
            return None
        return self._row_tokens(provider, row)

    async def all(self) -> dict[str, TokenSet]:
        rows = await self._pool.fetch(
            "SELECT provider, access_enc, refresh_enc, scopes, expires_at FROM oauth_tokens"
            " ORDER BY provider"
        )
        return {row["provider"]: self._row_tokens(row["provider"], row) for row in rows}

    def _row_tokens(self, provider: str, row: Any) -> TokenSet:
        try:
            return TokenSet(
                access_token=self._cipher.decrypt_text(bytes(row["access_enc"]), f"oauth_tokens:access_enc:{provider}"),
                refresh_token=self._cipher.decrypt_text(bytes(row["refresh_enc"]), f"oauth_tokens:refresh_enc:{provider}"),
                scopes=row["scopes"] or "",
                expires_at=row["expires_at"],
            )
        except CryptoError as exc:
            raise OAuthError(f"stored tokens for {provider} can't be decrypted: {exc}") from exc


class OAuthRefresher:
    """Keeps stored access tokens ahead of their expiry.

    A stale token self-heals anyway — a 401 breaks the MCP session and the
    reconnect re-resolves $GOOGLE_OAUTH_ACCESS_TOKEN through the store —
    this loop just keeps that rare. The client creds resolve fresh on
    every pass (acts read the resolver, never boot-time settings), so keys
    added after boot are picked up with no restart. SchedulerWorker's
    task pattern: a named task, stopped via Event + wait_for.
    """

    def __init__(
        self,
        store: OAuthTokenStore,
        *,
        resolver: Any,
        interval: float = 300.0,
        refresh_window: float = 300.0,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._interval = interval
        self._refresh_window = refresh_window
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="oauth-refresher")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("oauth refresh pass failed — retrying next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        log.info("oauth refresher stopped")

    async def tick(self) -> int:
        tokens_by_provider = await self._store.all()
        if not tokens_by_provider:
            return 0  # no sign-ins stored — nothing to do, no keys to read
        client_id = await self._resolver.resolve("GOOGLE_CLIENT_ID")
        client_secret = await self._resolver.resolve("GOOGLE_CLIENT_SECRET")
        if not client_id or not client_secret:
            return 0  # nothing to refresh with — the pass retries when they're in
        refreshed = 0
        horizon = datetime.now(UTC) + timedelta(seconds=self._refresh_window)
        for provider, tokens in tokens_by_provider.items():
            if not tokens.refresh_token or tokens.expires_at > horizon:
                continue
            try:
                fresh = await refresh_access(
                    provider,
                    refresh_token=tokens.refresh_token,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            except Exception as exc:
                log.warning("oauth refresh for %s failed — trying again next cycle: %s", provider, exc)
                continue
            await self._store.update_access(provider, fresh.access_token, fresh.expires_at)
            refreshed += 1
        return refreshed
