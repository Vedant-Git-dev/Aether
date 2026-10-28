"""ProviderRuntime: registration, effects, refresh, and polling."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import (
    AppDefinition,
    PollDefinition,
    ProviderAuthError,
    ProviderConnection,
    ProviderDefinition,
    ProviderHTTPError,
    ProviderReauthRequired,
    StaticTokenSetup,
    ToolDefinition,
)
from aether.secret_env import EnvResolver


async def _ping(ctx, params):
    return {"ok": True, "token": ctx.connection.access_token}


async def _poll(ctx):
    await ctx.set_sync("cursor", "2")
    return {"successful": True, "data": {"messages": [{"id": "1"}]}}


APP = AppDefinition(
    key="demo",
    name="Demo",
    provider="demo",
    scopes=("read",),
    tools=(
        ToolDefinition(
            key="ping",
            description="Ping the demo app",
            effect="read",
            required_scopes=("read",),
            input_schema={"type": "object", "properties": {}},
            handler=_ping,
        ),
    ),
    poll=PollDefinition(key="new", every_minutes=5.0, handler=_poll),
)

PROVIDER = ProviderDefinition(
    key="demo",
    name="Demo",
    api_base="https://api.demo",
    authorization_endpoint="https://auth.demo/authorize",
    token_endpoint="https://auth.demo/token",
    userinfo_endpoint="https://auth.demo/user",
    client_id_name="DEMO_CLIENT_ID",
    client_secret_name="DEMO_CLIENT_SECRET",
    apps=(APP,),
)

TOKEN_APP = AppDefinition(
    key="tokenapp",
    name="Token App",
    provider="tokenprovider",
    scopes=(),
    tools=(
        ToolDefinition(
            key="ping",
            description="Ping the token app",
            effect="read",
            input_schema={"type": "object", "properties": {}},
            handler=_ping,
            available_in=("token",),
        ),
    ),
)

TOKEN_PROVIDER = ProviderDefinition(
    key="tokenprovider",
    name="Token Provider",
    auth_modes=("token",),
    default_auth_mode="token",
    userinfo_endpoint="https://auth.demo/user",
    static_token=StaticTokenSetup(
        token_label="fine-grained token",
        token_prefix="github_pat_",
        expires=True,
        instructions=("Create a fine-grained token.",),
        permissions=("Metadata read",),
    ),
    apps=(TOKEN_APP,),
)


class FakeStore:
    def __init__(self, connection: ProviderConnection | None = None) -> None:
        self.connection = connection
        self.status = connection.status if connection is not None else ""
        self.sync: dict[tuple[str, str, str], str] = {}

    async def list(self):
        return [self.connection] if self.connection is not None else []

    async def get(self, provider: str):
        return (
            self.connection
            if self.connection is not None and self.connection.provider == provider
            else None
        )

    async def set_status(self, provider: str, status: str, error: str = "") -> None:
        self.status = status
        if self.connection is not None:
            self.connection.status = status
            self.connection.last_error = error

    async def upsert(self, connection: ProviderConnection):
        self.connection = connection
        return connection

    async def delete(self, provider: str) -> None:
        self.connection = None

    async def get_sync(self, provider: str, app: str, key: str):
        return self.sync.get((provider, app, key))

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        self.sync[(provider, app, key)] = value


class FakeOAuth:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.refreshed = 0
        self.last_auth: dict = {}
        self.revoked: list[str] = []

    async def client_configured(self, provider) -> bool:
        return self.configured

    async def authorization_url(self, provider, app, **kwargs) -> str:
        self.last_auth = kwargs
        return "https://auth.demo/authorize?state=x"

    async def identity(self, provider, token: str):
        return "acct-1", "me@example.com"

    async def revoke(self, provider, token: str) -> None:
        self.revoked.append(token)

    async def refresh(self, provider, connection: ProviderConnection):
        self.refreshed += 1
        connection.access_token = "new-token"
        connection.expires_at = datetime.now(UTC) + timedelta(hours=1)
        return connection


class FakeApprovals:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []

    async def fail_pending_matching(self, tool_prefix: str, reason: str) -> int:
        self.failed.append((tool_prefix, reason))
        return 1


def _runtime(
    store: FakeStore,
    oauth: FakeOAuth,
    handler,
    *,
    definitions=None,
    secrets=None,
    approvals=None,
) -> ProviderRuntime:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProviderRuntime(
        definitions=definitions or [PROVIDER],
        store=store,  # type: ignore[arg-type]
        oauth=oauth,  # type: ignore[arg-type]
        http=client,
        registry=ToolRegistry(),
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver(env_file="/nonexistent"),
        config=AppConfig(),
        public_base_url="https://aether.test",
        secrets=secrets,
        approvals=approvals,
    )


@pytest.mark.asyncio
async def test_register_tools_uses_the_provider_effect_and_scope() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        access_token="token",
        scopes="read",
        status="active",
    )
    runtime = _runtime(FakeStore(connection), FakeOAuth(), lambda request: httpx.Response(200))
    assert await runtime.register_tools() == 1
    tool = runtime._registry.get("demo__ping")  # noqa: SLF001 - test seam
    assert tool is not None
    assert tool.kind == "provider"
    assert tool.effect == "read"
    assert tool.auth_mode == "oauth"


@pytest.mark.asyncio
async def test_feed_reports_the_identity_and_setup_state() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        account_label="me@example.com",
        access_token="token",
        scopes="read",
        status="active",
    )
    runtime = _runtime(FakeStore(connection), FakeOAuth(), lambda request: httpx.Response(200))
    feed = await runtime.feed()
    assert feed["configured"] is True
    app = feed["apps"][0]
    assert app["key"] == "demo"
    assert app["connected"] is True
    assert app["identity"] == "me@example.com"
    assert app["actions"] == 1
    assert app["setup_required"] is False


@pytest.mark.asyncio
async def test_connect_returns_setup_or_a_link() -> None:
    runtime = _runtime(
        FakeStore(), FakeOAuth(configured=False), lambda request: httpx.Response(200)
    )
    result = await runtime.connect("demo")
    assert result.setup_required is True
    assert result.redirect_uri == "https://aether.test/api/oauth/demo/callback"

    runtime = _runtime(FakeStore(), FakeOAuth(configured=True), lambda request: httpx.Response(200))
    result = await runtime.connect("demo")
    assert result.redirect_url == "https://auth.demo/authorize?state=x"


@pytest.mark.asyncio
async def test_adding_a_scope_reuses_the_connection_and_asks_for_the_union() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        app="demo",
        access_token="old",
        refresh_token="refresh",
        scopes="read",
        status="active",
    )
    wider_app = replace(APP, scopes=("read", "write"))
    provider = replace(PROVIDER, apps=(wider_app,))
    oauth = FakeOAuth()
    runtime = _runtime(
        FakeStore(connection),
        oauth,
        lambda request: httpx.Response(200),
        definitions=[provider],
    )
    await runtime.connect("demo")
    assert oauth.last_auth["existing_scopes"] == ("read",)
    assert oauth.last_auth["prompt_consent"] is True


@pytest.mark.asyncio
async def test_reconnecting_a_dead_connection_forces_a_fresh_consent() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        app="demo",
        access_token="old",
        refresh_token="dead",
        scopes="read",
        status="reauth_required",
    )
    oauth = FakeOAuth()
    runtime = _runtime(FakeStore(connection), oauth, lambda request: httpx.Response(200))
    await runtime.connect("demo")
    assert oauth.last_auth["prompt_consent"] is True

    connection.status = "active"
    oauth.last_auth = {}
    await runtime.connect("demo")
    assert oauth.last_auth["prompt_consent"] is False


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_retries() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        access_token="old-token",
        refresh_token="refresh",
        scopes="read",
        status="active",
    )
    store = FakeStore(connection)
    oauth = FakeOAuth()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": True})

    runtime = _runtime(store, oauth, handler)
    response = await runtime.request(connection, "GET", "https://api.demo/thing")
    assert response.status_code == 200
    assert seen == ["Bearer old-token", "Bearer new-token"]
    assert oauth.refreshed == 1


@pytest.mark.asyncio
async def test_a_5xx_read_is_retried_once() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="token",
        access_token="pat",
        status="active",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"ok": True})

    runtime = _runtime(FakeStore(connection), FakeOAuth(), handler)
    response = await runtime.request(connection, "GET", "https://api.demo/thing")
    assert response.status_code == 200
    assert calls == ["GET", "GET"]


@pytest.mark.asyncio
async def test_a_write_timeout_says_the_outcome_is_unknown() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="token",
        access_token="pat",
        status="active",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    runtime = _runtime(FakeStore(connection), FakeOAuth(), handler)
    with pytest.raises(ProviderHTTPError) as caught:
        await runtime.request(connection, "POST", "https://api.demo/send")
    assert "can't tell whether that action went through" in caught.value.plain


@pytest.mark.asyncio
async def test_a_5xx_write_is_never_retried() -> None:
    """A send may have left Aether before the 5xx landed — retrying it
    would be a duplicate, so the outcome is reported as unknown instead."""
    connection = ProviderConnection(
        provider="demo",
        auth_mode="token",
        access_token="pat",
        status="active",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503, json={"error": "down"})

    runtime = _runtime(FakeStore(connection), FakeOAuth(), handler)
    with pytest.raises(ProviderHTTPError):
        await runtime.request(connection, "POST", "https://api.demo/send")
    assert calls == ["POST"]


@pytest.mark.asyncio
async def test_a_token_mode_401_marks_reauth_and_drops_tools() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="token",
        access_token="pat",
        scopes="",
        status="active",
    )
    store = FakeStore(connection)
    approvals = FakeApprovals()
    runtime = _runtime(store, FakeOAuth(), lambda request: httpx.Response(401), approvals=approvals)
    await runtime.register_tools()
    with pytest.raises(ProviderReauthRequired):
        await runtime.request(connection, "GET", "https://api.demo/thing")
    assert store.status == "reauth_required"
    assert runtime._registry.get("demo__ping") is None  # noqa: SLF001 - test seam
    assert approvals.failed == [("demo__", "demo needs reconnecting — the held action did not run")]


@pytest.mark.asyncio
async def test_poll_runs_once_then_waits_for_the_interval() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        access_token="token",
        scopes="read",
        status="active",
    )
    store = FakeStore(connection)
    runtime = _runtime(store, FakeOAuth(), lambda request: httpx.Response(200))
    now = datetime.now(UTC)
    results = await runtime.poll_due(now)
    assert len(results) == 1
    assert results[0].payload["data"]["messages"] == [{"id": "1"}]
    assert store.sync[("demo", "demo", "poll:new:last")]
    assert await runtime.poll_due(now + timedelta(minutes=1)) == []


class FakeSecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


@pytest.mark.asyncio
async def test_disconnect_drops_tools_and_fails_the_apps_pending_approvals() -> None:
    connection = ProviderConnection(
        provider="demo",
        auth_mode="oauth",
        app="demo",
        access_token="token",
        refresh_token="refresh",
        scopes="read",
        status="active",
    )
    store = FakeStore(connection)
    approvals = FakeApprovals()
    oauth = FakeOAuth()
    runtime = _runtime(
        store,
        oauth,
        lambda request: httpx.Response(200, json={}),
        approvals=approvals,
    )
    assert await runtime.register_tools() == 1
    assert runtime._registry.get("demo__ping") is not None

    await runtime.disconnect("demo")

    assert store.connection is None
    assert runtime._registry.get("demo__ping") is None
    assert oauth.revoked == ["token", "refresh"]
    assert approvals.failed == [("demo__", "demo was disconnected — the held action did not run")]


@pytest.mark.asyncio
async def test_static_token_connects_without_a_callback() -> None:
    store = FakeStore()
    runtime = _runtime(
        store,
        FakeOAuth(),
        lambda request: httpx.Response(200),
        definitions=[TOKEN_PROVIDER],
    )
    connection = await runtime.connect_token("tokenapp", "github_pat_abc")
    assert connection.auth_mode == "token"
    assert connection.account_label == "me@example.com"
    assert store.connection is connection
    assert runtime._registry.get("tokenapp__ping") is not None  # noqa: SLF001 - test seam

    with pytest.raises(ProviderAuthError):
        await runtime.connect_token("tokenapp", "ghp_classic")


@pytest.mark.asyncio
async def test_save_client_writes_both_names_and_never_echoes_values() -> None:
    secrets = FakeSecrets()
    runtime = _runtime(
        FakeStore(),
        FakeOAuth(),
        lambda request: httpx.Response(200),
        secrets=secrets,  # type: ignore[arg-type]
    )
    recipe = await runtime.save_client("demo", "client-id", "client-secret")
    assert secrets.values == {
        "DEMO_CLIENT_ID": "client-id",
        "DEMO_CLIENT_SECRET": "client-secret",
    }
    assert recipe["configured"] is True
    assert "client-secret" not in str(recipe)
