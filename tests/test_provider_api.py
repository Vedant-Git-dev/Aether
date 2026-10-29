"""The direct-provider API: feed, connect, callback, disconnect."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from aether.api.routes import router
from aether.config import Settings
from aether.providers.types import ConnectResult, ProviderConnection


class FakeRuntime:
    def __init__(self) -> None:
        self.connected: list[tuple[str, str]] = []
        self.disconnected: list[str] = []
        self.token_calls: list[tuple[str, str]] = []
        self.connect_modes: list[tuple[str, str | None]] = []

    async def feed(self) -> dict:
        return {
            "configured": True,
            "apps": [
                {
                    "key": "gmail",
                    "name": "Gmail",
                    "provider": "google",
                    "connected": True,
                    "status": "active",
                    "identity": "me@example.com",
                    "auth_mode": "oauth",
                    "scopes": ["gmail.readonly"],
                    "actions": 6,
                    "setup_required": False,
                    "setup_url": "https://console.cloud.google.com/apis/credentials",
                    "setup_steps": [],
                }
            ],
            "providers": [],
        }

    async def setup_recipe(self, provider: str) -> dict:
        return {"provider": provider, "configured": False, "redirect_uri": "https://x/cb"}

    async def save_client(self, provider: str, client_id: str, client_secret: str) -> dict:
        assert client_secret
        return {"provider": provider, "configured": True, "redirect_uri": "https://x/cb"}

    async def connect(self, app: str, *, auth_mode: str | None = None) -> ConnectResult:
        if app == "missing":
            from aether.providers.types import ProviderNotConfigured

            raise ProviderNotConfigured("no such app")
        self.connect_modes.append((app, auth_mode))
        return ConnectResult(
            app=app,
            provider="google",
            redirect_url="https://accounts.example/auth",
            redirect_uri="https://x/cb",
        )

    async def handle_callback(self, provider: str, *, code: str, state: str) -> ProviderConnection:
        if state == "bad":
            from aether.providers.types import ProviderAuthError

            raise ProviderAuthError("the sign-in expired")
        self.connected.append((provider, code))
        return ProviderConnection(
            provider=provider,
            auth_mode="oauth",
            app="gmail",
            account_label="me@example.com",
            access_token="token",
        )

    async def connect_token(self, app: str, token: str, *, expires_at=None) -> ProviderConnection:
        self.token_calls.append((app, token))
        return ProviderConnection(
            provider="github",
            auth_mode="token",
            app=app,
            account_label="octocat",
            access_token=token,
        )

    async def disconnect(self, app: str) -> None:
        self.disconnected.append(app)


class FakeAgent:
    def __init__(self) -> None:
        self.announced: list[tuple[str, str]] = []

    async def announce_provider_connection(self, app: str, identity: str = "") -> None:
        self.announced.append((app, identity))


def _app(runtime: FakeRuntime | None = None, agent: FakeAgent | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings.without_env_file(api_token="secret")
    app.state.provider_runtime = runtime or FakeRuntime()
    if agent is not None:
        app.state.agent = agent
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_provider_feed_requires_the_token() -> None:
    async with _client(_app()) as client:
        assert (await client.get("/api/providers")).status_code == 401
        response = await client.get("/api/providers", params={"token": "secret"})
    assert response.status_code == 200
    assert response.json()["apps"][0]["key"] == "gmail"


async def test_setup_recipe_is_token_guarded_and_returns_the_redirect_uri() -> None:
    async with _client(_app()) as client:
        assert (await client.get("/api/providers/google/setup")).status_code == 401
        response = await client.get("/api/providers/google/setup", params={"token": "secret"})
    assert response.status_code == 200
    assert response.json()["redirect_uri"] == "https://x/cb"


async def test_connect_returns_a_link() -> None:
    runtime = FakeRuntime()
    async with _client(_app(runtime)) as client:
        response = await client.post("/api/providers/gmail/connect", params={"token": "secret"})
    assert response.status_code == 200
    assert response.json()["redirect_url"] == "https://accounts.example/auth"
    assert runtime.connect_modes == [("gmail", None)]


async def test_connect_forwards_an_explicit_auth_mode() -> None:
    runtime = FakeRuntime()
    async with _client(_app(runtime)) as client:
        response = await client.post(
            "/api/providers/github/connect",
            params={"token": "secret", "mode": "oauth"},
        )
    assert response.status_code == 200
    assert runtime.connect_modes == [("github", "oauth")]


async def test_connect_error_is_plain() -> None:
    async with _client(_app()) as client:
        response = await client.post("/api/providers/missing/connect", params={"token": "secret"})
    assert response.status_code == 400
    assert response.json()["detail"] == "no such app"


async def test_callback_redirects_and_announces() -> None:
    runtime = FakeRuntime()
    agent = FakeAgent()
    async with _client(_app(runtime, agent)) as client:
        response = await client.get(
            "/api/oauth/google/callback", params={"code": "c", "state": "s"}
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/#/apps"
    assert runtime.connected == [("google", "c")]
    assert agent.announced == [("gmail", "me@example.com")]


async def test_callback_failure_is_plain_and_does_not_connect() -> None:
    runtime = FakeRuntime()
    async with _client(_app(runtime)) as client:
        response = await client.get(
            "/api/oauth/google/callback", params={"code": "c", "state": "bad"}
        )
    assert response.status_code == 400
    assert "the sign-in expired" in response.text
    assert runtime.connected == []


async def test_token_connect_and_disconnect() -> None:
    runtime = FakeRuntime()
    agent = FakeAgent()
    async with _client(_app(runtime, agent)) as client:
        response = await client.post(
            "/api/providers/github/token",
            params={"token": "secret"},
            json={"token": "github_pat_abc"},
        )
        assert response.status_code == 200
        assert response.json()["identity"] == "octocat"
        assert (
            await client.post("/api/providers/github/disconnect", params={"token": "secret"})
        ).status_code == 200
    assert runtime.token_calls == [("github", "github_pat_abc")]
    assert runtime.disconnected == ["github"]
    assert agent.announced == [("github", "octocat")]


async def test_client_setup_is_write_only_and_returns_the_recipe() -> None:
    async with _client(_app()) as client:
        response = await client.put(
            "/api/providers/google/client",
            params={"token": "secret"},
            json={"client_id": "id", "client_secret": "secret-value"},
        )
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert "secret-value" not in response.text
