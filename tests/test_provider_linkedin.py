"""LinkedIn handlers, identity, posting, and scope gating."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.linkedin import (
    LINKEDIN_APP,
    LINKEDIN_EMAIL,
    LINKEDIN_OPENID,
    LINKEDIN_POST,
    LINKEDIN_PROFILE,
    LINKEDIN_PROVIDER,
    create_post,
    get_profile,
)
from aether.providers.oauth import OAuthEngine
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import ProviderConnection
from aether.secret_env import EnvResolver


class FakeCtx:
    def __init__(self, account_id: str = "member-1") -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.connection = ProviderConnection(
            provider="linkedin",
            auth_mode="oauth",
            app="linkedin",
            account_id=account_id,
            account_label="me@example.com",
            access_token="access",
            status="active",
        )

    async def json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/v2/userinfo":
            return {
                "sub": "member-1",
                "name": "Ada Lovelace",
                "given_name": "Ada",
                "family_name": "Lovelace",
                "email": "ada@example.com",
                "email_verified": True,
                "picture": "https://example.com/ada.jpg",
                "locale": "en-US",
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    async def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:123"})


@pytest.mark.asyncio
async def test_get_profile_normalizes_the_openid_userinfo() -> None:
    ctx = FakeCtx()
    result = await get_profile(ctx, {})
    assert result["successful"] is True
    profile = result["data"]["profile"]
    assert profile["id"] == "member-1"
    assert profile["name"] == "Ada Lovelace"
    assert profile["email"] == "ada@example.com"
    assert profile["email_verified"] is True
    assert ctx.calls == [("GET", "/v2/userinfo", {})]


@pytest.mark.asyncio
async def test_create_post_builds_the_member_post_and_reads_the_post_id() -> None:
    ctx = FakeCtx()
    result = await create_post(ctx, {"text": "  Hello from Aether.  "})
    assert result["successful"] is True
    assert result["data"]["published"] is True
    assert result["data"]["id"] == "urn:li:share:123"
    assert result["data"]["url"] == "https://www.linkedin.com/feed/update/urn:li:share:123/"
    assert result["data"]["visibility"] == "PUBLIC"

    method, path, kwargs = ctx.calls[0]
    assert (method, path) == ("POST", "/rest/posts")
    assert kwargs["json"] == {
        "author": "urn:li:person:member-1",
        "commentary": "Hello from Aether.",
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    assert kwargs["headers"]["Linkedin-Version"] == "202610"
    assert kwargs["headers"]["X-Restli-Protocol-Version"] == "2.0.0"


@pytest.mark.asyncio
async def test_create_post_rejects_empty_text_and_non_public_visibility() -> None:
    assert (await create_post(FakeCtx(), {}))["successful"] is False
    assert (await create_post(FakeCtx(), {"text": "hello", "visibility": "CONNECTIONS"}))[
        "successful"
    ] is False


@pytest.mark.asyncio
async def test_create_post_requires_a_member_id() -> None:
    result = await create_post(FakeCtx(account_id=""), {"text": "hello"})
    assert result["successful"] is False
    assert "reconnected" in result["error"]


class _Store:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection

    async def list(self):
        return [self.connection]

    async def get(self, provider: str):
        return self.connection if provider == "linkedin" else None


class _OAuth:
    async def client_configured(self, provider) -> bool:
        return True


def _connection(scopes: tuple[str, ...]) -> ProviderConnection:
    return ProviderConnection(
        provider="linkedin",
        auth_mode="oauth",
        app="linkedin",
        account_id="member-1",
        account_label="me@example.com",
        access_token="access",
        scopes=" ".join(scopes),
        status="active",
    )


def _runtime(connection: ProviderConnection, registry: ToolRegistry, handler=None):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler
            or (lambda request: httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"}))
        )
    )
    return ProviderRuntime(
        definitions=[LINKEDIN_PROVIDER],
        store=_Store(connection),  # type: ignore[arg-type]
        oauth=_OAuth(),  # type: ignore[arg-type]
        http=client,
        registry=registry,
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver("/nonexistent"),
        config=AppConfig(),
        public_base_url="https://aether.test",
    )


@pytest.mark.asyncio
async def test_full_linkedin_scope_registers_identity_and_posting() -> None:
    registry = ToolRegistry()
    runtime = _runtime(
        _connection((LINKEDIN_OPENID, LINKEDIN_PROFILE, LINKEDIN_EMAIL, LINKEDIN_POST)),
        registry,
    )
    assert await runtime.register_tools() == 2
    assert registry.get("linkedin__get_profile") is not None
    assert registry.get("linkedin__create_post") is not None


@pytest.mark.asyncio
async def test_scopes_gate_identity_and_posting_separately() -> None:
    post_only = ToolRegistry()
    post_runtime = _runtime(_connection((LINKEDIN_POST,)), post_only)
    assert await post_runtime.register_tools() == 1
    assert post_only.get("linkedin__get_profile") is None
    assert post_only.get("linkedin__create_post") is not None

    identity_only = ToolRegistry()
    identity_runtime = _runtime(_connection((LINKEDIN_OPENID,)), identity_only)
    assert await identity_runtime.register_tools() == 1
    assert identity_only.get("linkedin__get_profile") is not None
    assert identity_only.get("linkedin__create_post") is None


@pytest.mark.asyncio
async def test_linkedin_tools_use_the_linkedin_api_base() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})

    registry = ToolRegistry()
    runtime = _runtime(_connection((LINKEDIN_POST,)), registry, handler=handler)
    await runtime.register_tools()
    await registry.execute("linkedin__create_post", {"text": "hello"})
    assert seen == ["https://api.linkedin.com/rest/posts"]


def test_linkedin_uses_the_web_oauth_flow_without_pkce_or_prompt() -> None:
    assert LINKEDIN_PROVIDER.pkce is False
    assert LINKEDIN_PROVIDER.prompt_consent_param is False
    assert LINKEDIN_PROVIDER.revocation_endpoint is None


class _StateStore:
    def __init__(self) -> None:
        self.state: dict = {}

    async def create_state(self, **kwargs) -> None:
        self.state = kwargs


class _Resolver:
    async def resolve(self, name: str):
        return {"LINKEDIN_CLIENT_ID": "client-id", "LINKEDIN_CLIENT_SECRET": "secret"}.get(name)


@pytest.mark.asyncio
async def test_linkedin_authorization_url_omits_prompt_and_pkce() -> None:
    store = _StateStore()
    engine = OAuthEngine(
        store=store,  # type: ignore[arg-type]
        resolver=_Resolver(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(),
        audit=FakeAudit(),  # type: ignore[arg-type]
    )
    url = await engine.authorization_url(
        LINKEDIN_PROVIDER,
        LINKEDIN_APP,
        redirect_uri="https://aether.test/api/oauth/linkedin/callback",
        prompt_consent=True,
    )
    query = parse_qs(urlparse(url).query)
    assert "prompt" not in query
    assert "code_challenge" not in query
    assert "code_challenge_method" not in query
    assert set(query["scope"][0].split()) == {
        LINKEDIN_OPENID,
        LINKEDIN_PROFILE,
        LINKEDIN_EMAIL,
        LINKEDIN_POST,
    }
    assert store.state["code_verifier"] == ""
