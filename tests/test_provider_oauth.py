"""The OAuth engine: authorization, exchange, replay rejection, refresh."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fakes import FakeAudit

from aether.providers.oauth import OAuthEngine
from aether.providers.store import OAuthState
from aether.providers.types import (
    AppDefinition,
    ProviderAuthError,
    ProviderConnection,
    ProviderDefinition,
    ProviderNotConfigured,
    ToolDefinition,
)


async def _noop(ctx, params):
    return {}


APP = AppDefinition(
    key="demo",
    name="Demo",
    provider="demo",
    scopes=("read", "write"),
    tools=(
        ToolDefinition(
            key="ping",
            description="Ping",
            effect="read",
            input_schema={"type": "object", "properties": {}},
            handler=_noop,
        ),
    ),
)

PROVIDER = ProviderDefinition(
    key="demo",
    name="Demo",
    authorization_endpoint="https://auth.demo/authorize",
    token_endpoint="https://auth.demo/token",
    userinfo_endpoint="https://auth.demo/user",
    client_id_name="DEMO_CLIENT_ID",
    client_secret_name="DEMO_CLIENT_SECRET",
    base_scopes=("openid", "email"),
    extra_auth_params={"access_type": "offline"},
    apps=(APP,),
)


class FakeResolver:
    def __init__(self) -> None:
        self.values = {"DEMO_CLIENT_ID": "client-id", "DEMO_CLIENT_SECRET": "client-secret"}

    async def resolve(self, name: str):
        return self.values.get(name)


class FakeStore:
    def __init__(self) -> None:
        self.states: dict[str, OAuthState] = {}
        self.connections: dict[str, ProviderConnection] = {}
        self.updated: list[dict] = []
        self.deleted: list[str] = []

    async def create_state(self, **kwargs) -> None:
        self.states[kwargs["state_hash"]] = OAuthState(
            state_hash=kwargs["state_hash"],
            provider=kwargs["provider"],
            app=kwargs["app"],
            scopes=kwargs["scopes"],
            code_verifier=kwargs["code_verifier"],
            redirect_uri=kwargs["redirect_uri"],
        )

    async def consume_state(self, state_hash: str):
        state = self.states.pop(state_hash, None)
        return state

    async def get(self, provider: str):
        return self.connections.get(provider)

    async def upsert(self, connection: ProviderConnection):
        self.connections[connection.provider] = connection
        return connection

    async def update_tokens(self, provider: str, **kwargs) -> str:
        self.updated.append(kwargs)
        return "v1"

    async def delete(self, provider: str) -> None:
        self.deleted.append(provider)
        self.connections.pop(provider, None)


def _engine(store: FakeStore, handler, resolver=None) -> OAuthEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OAuthEngine(
        store=store,  # type: ignore[arg-type]
        resolver=resolver or FakeResolver(),  # type: ignore[arg-type]
        http=client,
        audit=FakeAudit(),  # type: ignore[arg-type]
    )


def _token_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/token":
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "read write openid email",
            },
        )
    if request.url.path == "/user":
        return httpx.Response(200, json={"sub": "acct-1", "email": "me@example.com"})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_authorization_url_carries_state_pkce_and_scopes() -> None:
    store = FakeStore()
    engine = _engine(store, _token_handler)
    url = await engine.authorization_url(
        PROVIDER,
        APP,
        redirect_uri="https://aether.test/api/oauth/demo/callback",
        prompt_consent=True,
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "auth.demo"
    assert query["client_id"] == ["client-id"]
    assert query["state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]
    assert query["prompt"] == ["consent"]
    assert query["access_type"] == ["offline"]
    scope = set(query["scope"][0].split())
    assert {"read", "write", "openid", "email"} <= scope
    assert len(store.states) == 1


@pytest.mark.asyncio
async def test_authorization_url_keeps_existing_scopes_when_adding_an_app() -> None:
    """Calendar rides the Gmail connection. The consent request must
    carry the scopes already granted, or Google would replace them."""
    store = FakeStore()
    engine = _engine(store, lambda request: httpx.Response(200, json={}))
    url = await engine.authorization_url(
        PROVIDER,
        APP,
        redirect_uri="https://aether.test/api/oauth/demo/callback",
        existing_scopes=("read",),
        prompt_consent=True,
    )
    query = parse_qs(urlparse(url).query)
    assert {"read", "write", "openid", "email"}.issubset(set(query["scope"][0].split()))
    assert query["prompt"] == ["consent"]


@pytest.mark.asyncio
async def test_exchange_stores_tokens_and_rejects_a_replay() -> None:
    store = FakeStore()
    engine = _engine(store, _token_handler)
    url = await engine.authorization_url(
        PROVIDER, APP, redirect_uri="https://aether.test/api/oauth/demo/callback"
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    connection = await engine.exchange(PROVIDER, code="code-1", state=state)
    assert connection.access_token == "access-1"
    assert connection.refresh_token == "refresh-1"
    assert connection.account_label == "me@example.com"
    assert connection.app == "demo"
    assert connection.expires_at is not None
    assert store.connections["demo"] is connection

    with pytest.raises(ProviderAuthError):
        await engine.exchange(PROVIDER, code="code-1", state=state)


@pytest.mark.asyncio
async def test_exchange_replaces_a_static_token_connection() -> None:
    store = FakeStore()
    store.connections["demo"] = ProviderConnection(
        provider="demo",
        auth_mode="token",
        access_token="pat",
        scopes="Metadata read",
    )
    engine = _engine(store, _token_handler)
    url = await engine.authorization_url(
        PROVIDER, APP, redirect_uri="https://aether.test/api/oauth/demo/callback"
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    connection = await engine.exchange(PROVIDER, code="code-1", state=state)
    assert store.deleted == ["demo"]
    assert connection.auth_mode == "oauth"
    assert store.connections["demo"] is connection


@pytest.mark.asyncio
async def test_exchange_normalizes_comma_scopes_and_stores_refresh_expiry() -> None:
    store = FakeStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15552000,
                    "token_type": "bearer",
                    "scope": "repo,read:user,notifications",
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"sub": "acct-1", "email": "me@example.com"})
        return httpx.Response(404)

    engine = _engine(store, handler)
    url = await engine.authorization_url(
        PROVIDER, APP, redirect_uri="https://aether.test/api/oauth/demo/callback"
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    connection = await engine.exchange(PROVIDER, code="code-1", state=state)
    assert connection.scopes == "repo read:user notifications"
    assert connection.refresh_expires_at is not None
    assert connection.refresh_expires_at > connection.expires_at  # type: ignore[operator]


@pytest.mark.asyncio
async def test_refresh_stores_a_rotating_refresh_token() -> None:
    store = FakeStore()
    engine = _engine(
        store,
        lambda request: httpx.Response(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "refresh_token_expires_in": 15552000,
                "scope": "read",
            },
        ),
    )
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        access_token="access-1",
        refresh_token="refresh-1",
        scopes="read",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    refreshed = await engine.refresh(PROVIDER, connection)
    assert refreshed.access_token == "access-2"
    assert refreshed.refresh_token == "refresh-2"
    assert store.updated[0]["refresh_token"] == "refresh-2"
    assert store.updated[0]["access_token"] == "access-2"
    assert store.updated[0]["refresh_expires_at"] is not None
    assert refreshed.refresh_expires_at is not None


@pytest.mark.asyncio
async def test_exchange_keeps_the_stored_refresh_token_when_google_omits_it() -> None:
    """Adding a scope re-consents, but Google only hands the refresh token
    back on the first consent (or a forced one). The stored one must
    survive that reconnect."""
    store = FakeStore()
    store.connections["demo"] = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        access_token="old-access",
        refresh_token="refresh-1",
        scopes="read",
    )

    def no_refresh(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "expires_in": 3600,
                    "scope": "read write openid email",
                },
            )
        return httpx.Response(200, json={"sub": "acct-1", "email": "me@example.com"})

    engine = _engine(store, no_refresh)
    url = await engine.authorization_url(
        PROVIDER, APP, redirect_uri="https://aether.test/api/oauth/demo/callback"
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    connection = await engine.exchange(PROVIDER, code="code-2", state=state)
    assert connection.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_missing_client_is_reported_without_a_network_call() -> None:
    store = FakeStore()
    resolver = FakeResolver()
    resolver.values = {}
    engine = _engine(store, lambda request: httpx.Response(500), resolver=resolver)
    with pytest.raises(ProviderNotConfigured):
        await engine.authorization_url(PROVIDER, APP, redirect_uri="https://aether.test/callback")


@pytest.mark.asyncio
async def test_a_client_auth_revocation_endpoint_deletes_the_grant() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    provider = replace(
        PROVIDER,
        revocation_endpoint="https://api.github.com/applications/{client_id}/grant",
    )
    engine = _engine(FakeStore(), handler)
    await engine.revoke(provider, "access-1")
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/applications/client-id/grant"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert b"access_token" in seen[0].content
