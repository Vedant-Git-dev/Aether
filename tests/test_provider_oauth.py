"""The OAuth engine: authorization, exchange, replay rejection, refresh."""

from __future__ import annotations

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

    async def upsert(self, connection: ProviderConnection):
        self.connections[connection.provider] = connection
        return connection

    async def update_tokens(self, provider: str, **kwargs) -> str:
        self.updated.append(kwargs)
        return "v1"


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


@pytest.mark.asyncio
async def test_missing_client_is_reported_without_a_network_call() -> None:
    store = FakeStore()
    resolver = FakeResolver()
    resolver.values = {}
    engine = _engine(store, lambda request: httpx.Response(500), resolver=resolver)
    with pytest.raises(ProviderNotConfigured):
        await engine.authorization_url(PROVIDER, APP, redirect_uri="https://aether.test/callback")
