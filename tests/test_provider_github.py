"""GitHub handlers, auth-mode gating, and token validation."""

from __future__ import annotations

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.github import (
    GITHUB_NOTIFICATIONS,
    GITHUB_PROVIDER,
    _validate_token,
    comment_on_issue,
    comment_on_pull_request,
    create_issue,
    get_issue,
    get_pull_request,
    list_issues,
    list_notifications,
    list_pull_requests,
    search,
)
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import ProviderAuthError, ProviderConnection
from aether.secret_env import EnvResolver


class FakeCtx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/repos/acme/widgets/issues":
            return [
                {
                    "number": 7,
                    "title": "Crash on save",
                    "state": "open",
                    "body": "Steps to reproduce",
                    "user": {"login": "ada"},
                    "labels": [{"name": "bug"}, {"name": "p1"}],
                    "assignees": [{"login": "grace"}],
                    "milestone": {"title": "v1"},
                    "comments": 2,
                    "html_url": "https://github.com/acme/widgets/issues/7",
                },
                {
                    "number": 8,
                    "title": "A pull request",
                    "state": "open",
                    "pull_request": {"url": "https://api.github.com/pulls/8"},
                },
            ]
        if method == "GET" and path == "/repos/acme/widgets/issues/7":
            return {
                "number": 7,
                "title": "Crash on save",
                "state": "open",
                "user": {"login": "ada"},
            }
        if method == "POST" and path == "/repos/acme/widgets/issues":
            return {**kwargs["json"], "number": 9, "state": "open"}
        if method == "POST" and path in (
            "/repos/acme/widgets/issues/7/comments",
            "/repos/acme/widgets/issues/12/comments",
        ):
            number = 7 if path.endswith("/7/comments") else 12
            return {
                "id": 44,
                "body": kwargs["json"]["body"],
                "user": {"login": "ada"},
                "html_url": f"https://github.com/acme/widgets/issues/{number}#issuecomment-44",
            }
        if method == "GET" and path == "/repos/acme/widgets/pulls":
            return [
                {
                    "number": 12,
                    "title": "Add search",
                    "state": "open",
                    "user": {"login": "ada"},
                    "head": {"ref": "feature/search"},
                    "base": {"ref": "main"},
                    "draft": True,
                    "comments": 1,
                    "review_comments": 3,
                    "commits": 2,
                    "html_url": "https://github.com/acme/widgets/pull/12",
                }
            ]
        if method == "GET" and path == "/repos/acme/widgets/pulls/12":
            return {
                "number": 12,
                "title": "Add search",
                "state": "open",
                "head": {"ref": "feature/search"},
                "base": {"ref": "main"},
                "merged": False,
            }
        if method == "GET" and path == "/notifications":
            return [
                {
                    "id": "99",
                    "reason": "review_requested",
                    "unread": True,
                    "updated_at": "2026-10-29T10:00:00Z",
                    "subject": {
                        "title": "Add search",
                        "type": "PullRequest",
                        "url": "https://api.github.com/repos/acme/widgets/pulls/12",
                    },
                    "repository": {"full_name": "acme/widgets"},
                    "url": "https://api.github.com/notifications/threads/99",
                }
            ]
        if method == "GET" and path == "/search/issues":
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 7,
                        "title": "Crash on save",
                        "state": "open",
                        "pull_request": {"url": "https://api.github.com/pulls/7"},
                    }
                ],
            }
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.mark.asyncio
async def test_list_issues_filters_out_pull_requests_and_normalizes() -> None:
    ctx = FakeCtx()
    result = await list_issues(
        ctx,
        {
            "repo": "acme/widgets",
            "state": "all",
            "labels": ["bug", "p1"],
            "per_page": 500,
        },
    )
    assert result["successful"] is True
    assert result["data"]["count"] == 1
    issue = result["data"]["issues"][0]
    assert issue["number"] == 7
    assert issue["labels"] == ["bug", "p1"]
    assert issue["assignees"] == ["grace"]
    assert issue["milestone"] == "v1"
    assert issue["is_pull_request"] is False

    method, path, kwargs = ctx.calls[0]
    assert (method, path) == ("GET", "/repos/acme/widgets/issues")
    assert kwargs["params"] == {
        "state": "all",
        "labels": "bug,p1",
        "per_page": 100,
    }
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.asyncio
async def test_get_issue_requires_a_repository_and_number() -> None:
    assert (await get_issue(FakeCtx(), {"repo": "acme/widgets"}))["successful"] is False
    assert (await get_issue(FakeCtx(), {"repo": "bad", "number": 7}))["successful"] is False

    ctx = FakeCtx()
    result = await get_issue(ctx, {"repo": "acme/widgets", "number": 7})
    assert result["successful"] is True
    assert result["data"]["issue"]["title"] == "Crash on save"
    assert ctx.calls[0][:2] == ("GET", "/repos/acme/widgets/issues/7")
    assert ctx.calls[0][2]["headers"]["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_create_issue_posts_labels_assignees_and_milestone() -> None:
    ctx = FakeCtx()
    result = await create_issue(
        ctx,
        {
            "repo": "acme/widgets",
            "title": "Add search",
            "body": "Please add it.",
            "labels": ["enhancement"],
            "assignees": ["ada", "grace"],
            "milestone": 3,
        },
    )
    assert result["successful"] is True
    assert result["data"]["issue"]["number"] == 9
    method, path, kwargs = ctx.calls[0]
    assert (method, path) == ("POST", "/repos/acme/widgets/issues")
    assert kwargs["json"] == {
        "title": "Add search",
        "body": "Please add it.",
        "labels": ["enhancement"],
        "assignees": ["ada", "grace"],
        "milestone": 3,
    }


@pytest.mark.asyncio
async def test_comment_on_issue_posts_the_comment() -> None:
    ctx = FakeCtx()
    result = await comment_on_issue(
        ctx, {"repo": "acme/widgets", "number": 7, "body": "Looking into it."}
    )
    assert result["successful"] is True
    assert result["data"]["comment"]["body"] == "Looking into it."
    assert ctx.calls[0][:2] == ("POST", "/repos/acme/widgets/issues/7/comments")
    assert ctx.calls[0][2]["json"] == {"body": "Looking into it."}


@pytest.mark.asyncio
async def test_list_pull_requests_normalizes_branches_and_draft() -> None:
    ctx = FakeCtx()
    result = await list_pull_requests(ctx, {"repo": "acme/widgets", "state": "open"})
    assert result["successful"] is True
    pull = result["data"]["pull_requests"][0]
    assert pull["number"] == 12
    assert pull["head"] == "feature/search"
    assert pull["base"] == "main"
    assert pull["draft"] is True
    assert pull["review_comments"] == 3
    assert ctx.calls[0][:2] == ("GET", "/repos/acme/widgets/pulls")


@pytest.mark.asyncio
async def test_get_pull_request_and_comment_use_the_expected_paths() -> None:
    ctx = FakeCtx()
    result = await get_pull_request(ctx, {"repo": "acme/widgets", "number": 12})
    assert result["successful"] is True
    assert result["data"]["pull_request"]["base"] == "main"

    result = await comment_on_pull_request(
        ctx, {"repo": "acme/widgets", "number": 12, "body": "Please rebase."}
    )
    assert result["successful"] is True
    assert ctx.calls[-1][:2] == ("POST", "/repos/acme/widgets/issues/12/comments")


@pytest.mark.asyncio
async def test_list_notifications_normalizes_the_subject_and_repository() -> None:
    ctx = FakeCtx()
    result = await list_notifications(ctx, {"all": True, "per_page": 10})
    assert result["successful"] is True
    note = result["data"]["notifications"][0]
    assert note["reason"] == "review_requested"
    assert note["subject"]["type"] == "PullRequest"
    assert note["repository"] == "acme/widgets"
    assert ctx.calls[0][2]["params"] == {"all": "true", "per_page": 10}


@pytest.mark.asyncio
async def test_search_returns_total_count_and_incomplete_flag() -> None:
    ctx = FakeCtx()
    result = await search(ctx, {"query": "repo:acme/widgets is:open bug"})
    assert result["successful"] is True
    assert result["data"]["total_count"] == 1
    assert result["data"]["incomplete_results"] is False
    assert result["data"]["items"][0]["is_pull_request"] is True
    assert ctx.calls[0][2]["params"] == {"q": "repo:acme/widgets is:open bug"}


@pytest.mark.asyncio
async def test_missing_required_fields_are_errors_not_successes() -> None:
    assert (await list_issues(FakeCtx(), {}))["successful"] is False
    assert (await create_issue(FakeCtx(), {"repo": "acme/widgets"}))["successful"] is False
    assert (await comment_on_issue(FakeCtx(), {"repo": "acme/widgets", "number": 7}))[
        "successful"
    ] is False
    assert (await list_notifications(FakeCtx(), {}))["successful"] is True
    assert (await search(FakeCtx(), {}))["successful"] is False


class _Store:
    def __init__(self, connection: ProviderConnection | None = None) -> None:
        self.connection = connection
        self.deleted: list[str] = []

    async def list(self):
        return [self.connection] if self.connection is not None else []

    async def get(self, provider: str):
        if self.connection is not None and self.connection.provider == provider:
            return self.connection
        return None

    async def upsert(self, connection: ProviderConnection):
        self.connection = connection
        return connection

    async def delete(self, provider: str) -> None:
        self.deleted.append(provider)
        self.connection = None


class _OAuth:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.revoked: list[str] = []
        self.last_auth: dict = {}

    async def client_configured(self, provider) -> bool:
        return self.configured

    async def authorization_url(self, provider, app, **kwargs) -> str:
        self.last_auth = kwargs
        return "https://github.com/login/oauth/authorize?state=x"

    async def identity(self, provider, token: str):
        return "42", "octocat"

    async def revoke(self, provider, token: str) -> None:
        self.revoked.append(token)


def _runtime(
    store: _Store,
    oauth: _OAuth,
    handler=None,
) -> ProviderRuntime:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json=[{"name": "widgets"}]))
        )
    )
    return ProviderRuntime(
        definitions=[GITHUB_PROVIDER],
        store=store,  # type: ignore[arg-type]
        oauth=oauth,  # type: ignore[arg-type]
        http=client,
        registry=ToolRegistry(),
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver(env_file="/nonexistent"),
        config=AppConfig(),
        public_base_url="https://aether.test",
    )


def _token_connection() -> ProviderConnection:
    return ProviderConnection(
        provider="github",
        auth_mode="token",
        app="github",
        account_label="octocat",
        access_token="github_pat_old",
        scopes="Metadata: read-only; Issues: read and write; Pull requests: read and write",
        status="active",
    )


def _oauth_connection() -> ProviderConnection:
    return ProviderConnection(
        provider="github",
        auth_mode="oauth",
        app="github",
        account_label="octocat",
        access_token="old-access",
        refresh_token="old-refresh",
        scopes="read:user repo notifications read:org offline_access",
        status="active",
    )


@pytest.mark.asyncio
async def test_pat_mode_registers_eight_tools_and_oauth_registers_nine() -> None:
    token_runtime = _runtime(_Store(_token_connection()), _OAuth())
    assert await token_runtime.register_tools() == 8
    assert token_runtime._registry.get("github__list_issues") is not None  # noqa: SLF001
    assert token_runtime._registry.get("github__list_notifications") is None  # noqa: SLF001

    oauth_runtime = _runtime(_Store(_oauth_connection()), _OAuth())
    assert await oauth_runtime.register_tools() == 9
    assert oauth_runtime._registry.get("github__list_notifications") is not None  # noqa: SLF001

    no_notifications = _oauth_connection()
    no_notifications.scopes = "read:user repo offline_access"
    narrow_runtime = _runtime(_Store(no_notifications), _OAuth())
    assert await narrow_runtime.register_tools() == 8
    assert narrow_runtime._registry.get("github__list_notifications") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_github_defaults_to_a_token_and_oauth_is_an_explicit_choice() -> None:
    oauth = _OAuth(configured=True)
    runtime = _runtime(_Store(), oauth)

    result = await runtime.connect("github")
    assert result.token_required is True
    assert result.setup_url == "https://github.com/settings/tokens?type=beta"
    assert "Fine-grained tokens" in "\n".join(result.setup_steps)

    result = await runtime.connect("github", auth_mode="oauth")
    assert result.redirect_url.startswith("https://github.com/login/oauth/authorize")
    assert oauth.last_auth["existing_scopes"] == ()
    assert oauth.last_auth["prompt_consent"] is True

    with pytest.raises(ProviderAuthError):
        await runtime.connect("github", auth_mode="password")


@pytest.mark.asyncio
async def test_connect_token_validates_and_replaces_an_oauth_connection() -> None:
    store = _Store(_oauth_connection())
    oauth = _OAuth()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"name": "widgets"}])

    runtime = _runtime(store, oauth, handler)
    connection = await runtime.connect_token("github", "github_pat_new")

    assert connection.auth_mode == "token"
    assert connection.scopes.startswith("Metadata: read-only")
    assert store.deleted == ["github"]
    assert oauth.revoked == ["old-access", "old-refresh"]
    assert store.connection is connection
    assert seen[0].url.path == "/user/repos"
    assert seen[0].headers["authorization"] == "Bearer github_pat_new"


@pytest.mark.asyncio
async def test_token_disconnect_deletes_locally_without_an_oauth_revoke() -> None:
    store = _Store(_token_connection())
    oauth = _OAuth()
    runtime = _runtime(store, oauth)
    await runtime.disconnect("github")
    assert store.connection is None
    assert oauth.revoked == []


@pytest.mark.asyncio
async def test_a_rejected_token_does_not_replace_the_working_connection() -> None:
    store = _Store(_oauth_connection())
    oauth = _OAuth()
    runtime = _runtime(store, oauth, lambda request: httpx.Response(401))

    with pytest.raises(ProviderAuthError):
        await runtime.connect_token("github", "github_pat_bad")

    assert store.connection is not None
    assert store.connection.auth_mode == "oauth"
    assert oauth.revoked == []


@pytest.mark.asyncio
async def test_validate_token_accepts_a_repo_and_rejects_empty_or_denied() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[{"name": "widgets"}])
        )
    ) as client:
        await _validate_token(client, "github_pat_good")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        with pytest.raises(ProviderAuthError, match="can't see any repositories"):
            await _validate_token(client, "github_pat_empty")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401))
    ) as client:
        with pytest.raises(ProviderAuthError, match="didn't accept"):
            await _validate_token(client, "github_pat_bad")


@pytest.mark.asyncio
async def test_feed_exposes_the_token_recipe_and_both_auth_modes() -> None:
    runtime = _runtime(_Store(), _OAuth())
    feed = await runtime.feed()
    app = feed["apps"][0]
    assert app["auth_modes"] == ("token", "oauth")
    assert app["default_auth_mode"] == "token"
    assert app["token_label"] == "GitHub fine-grained personal access token"
    assert app["token_expires"] is True
    assert app["token_url"] == "https://github.com/settings/tokens?type=beta"
    assert "Fine-grained tokens" in "\n".join(app["token_steps"])


def test_the_github_app_requests_notifications_only_through_oauth() -> None:
    tools = {tool.key: tool for tool in GITHUB_PROVIDER.app("github").tools}  # type: ignore[union-attr]
    assert tools["list_notifications"].available_in == ("oauth",)
    assert tools["list_notifications"].required_scopes == (GITHUB_NOTIFICATIONS,)
    assert GITHUB_NOTIFICATIONS in GITHUB_PROVIDER.app("github").scopes  # type: ignore[union-attr]
    assert GITHUB_PROVIDER.default_auth_mode == "token"
    assert GITHUB_PROVIDER.static_token is not None
    assert GITHUB_PROVIDER.static_token.token_prefix == "github_pat_"
