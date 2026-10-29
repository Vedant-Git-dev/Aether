"""GitHub: issues, pull requests, search, and notifications.

A fine-grained personal access token is the default connection mode. An OAuth
App is optional and is the only mode that can read notifications.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .types import (
    AppDefinition,
    ProviderAuthError,
    ProviderDefinition,
    StaticTokenSetup,
    ToolDefinition,
)

GITHUB_API = "https://api.github.com"
GITHUB_READ_USER = "read:user"
GITHUB_REPO = "repo"
GITHUB_NOTIFICATIONS = "notifications"
GITHUB_READ_ORG = "read:org"
GITHUB_OFFLINE = "offline_access"

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_GITHUB_TOKEN_STEPS = (
    "Open GitHub Settings, Developer settings, Personal access tokens, Fine-grained tokens.",
    "Choose Generate new token and select only the repositories Aether may use.",
    "Grant Metadata read-only, Issues read and write, and Pull requests read and write.",
    "Choose an expiry, generate the token, and paste the github_pat_ value here.",
)

_GITHUB_SETUP_STEPS = (
    "Open GitHub Developer Settings and create a new OAuth App.",
    "Set the homepage URL to your Aether URL.",
    "Set the authorization callback URL to the redirect URI shown here.",
    "Enable expiring user tokens in the OAuth App settings if you want refresh.",
    "Copy the client ID and client secret into Aether when asked.",
)


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"successful": True, "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"successful": False, "error": message}


def _login(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("login") or value.get("name") or "")
    return str(value or "")


def _label_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for label in value:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
    return names


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


def _bounded_int(value: Any, *, maximum: int | None = None) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    if maximum is not None:
        number = min(number, maximum)
    return number


def _repo_path(params: dict[str, Any], suffix: str = "") -> str | None:
    repo = str(params.get("repo") or "").strip()
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        return None
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{suffix}"


def _number(params: dict[str, Any]) -> int | None:
    return _bounded_int(params.get("number"))


def _paging(params: dict[str, Any]) -> dict[str, int]:
    paging: dict[str, int] = {}
    per_page = _bounded_int(params.get("per_page"), maximum=100)
    page = _bounded_int(params.get("page"))
    if per_page is not None:
        paging["per_page"] = per_page
    if page is not None:
        paging["page"] = page
    return paging


def _state(params: dict[str, Any], default: str = "open") -> str:
    value = str(params.get("state") or "").strip().lower()
    return value if value in ("open", "closed", "all") else default


def _issue_view(item: dict[str, Any]) -> dict[str, Any]:
    milestone = item.get("milestone") or {}
    return {
        "number": int(item.get("number") or 0),
        "title": str(item.get("title") or ""),
        "state": str(item.get("state") or ""),
        "body": str(item.get("body") or ""),
        "user": _login(item.get("user")),
        "labels": _label_names(item.get("labels")),
        "assignees": [_login(person) for person in item.get("assignees") or []],
        "milestone": str(milestone.get("title") or "") if isinstance(milestone, dict) else "",
        "comments": int(item.get("comments") or 0),
        "is_pull_request": bool(item.get("pull_request")),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "closed_at": str(item.get("closed_at") or ""),
        "html_url": str(item.get("html_url") or ""),
    }


def _pull_view(item: dict[str, Any]) -> dict[str, Any]:
    head = item.get("head") or {}
    base = item.get("base") or {}
    return {
        "number": int(item.get("number") or 0),
        "title": str(item.get("title") or ""),
        "state": str(item.get("state") or ""),
        "body": str(item.get("body") or ""),
        "user": _login(item.get("user")),
        "labels": _label_names(item.get("labels")),
        "assignees": [_login(person) for person in item.get("assignees") or []],
        "requested_reviewers": [_login(person) for person in item.get("requested_reviewers") or []],
        "head": str(head.get("ref") or "") if isinstance(head, dict) else "",
        "base": str(base.get("ref") or "") if isinstance(base, dict) else "",
        "draft": bool(item.get("draft")),
        "merged": bool(item.get("merged")),
        "mergeable": item.get("mergeable"),
        "comments": int(item.get("comments") or 0),
        "review_comments": int(item.get("review_comments") or 0),
        "commits": int(item.get("commits") or 0),
        "additions": int(item.get("additions") or 0),
        "deletions": int(item.get("deletions") or 0),
        "changed_files": int(item.get("changed_files") or 0),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "closed_at": str(item.get("closed_at") or ""),
        "merged_at": str(item.get("merged_at") or ""),
        "html_url": str(item.get("html_url") or ""),
    }


def _comment_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "body": str(item.get("body") or ""),
        "user": _login(item.get("user")),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "html_url": str(item.get("html_url") or ""),
    }


def _notification_view(item: dict[str, Any]) -> dict[str, Any]:
    subject = item.get("subject") or {}
    repository = item.get("repository") or {}
    return {
        "id": str(item.get("id") or ""),
        "reason": str(item.get("reason") or ""),
        "unread": bool(item.get("unread")),
        "updated_at": str(item.get("updated_at") or ""),
        "subject": {
            "title": str(subject.get("title") or "") if isinstance(subject, dict) else "",
            "type": str(subject.get("type") or "") if isinstance(subject, dict) else "",
            "url": str(subject.get("url") or "") if isinstance(subject, dict) else "",
        },
        "repository": (
            str(repository.get("full_name") or "") if isinstance(repository, dict) else ""
        ),
        "url": str(item.get("url") or ""),
    }


async def _validate_token(http: Any, token: str) -> None:
    """Confirm the token and that it can see at least one repository."""

    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        response = await http.get(
            f"{GITHUB_API}/user/repos",
            params={"per_page": 1},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise ProviderAuthError("GitHub didn't answer while checking that token") from exc
    if response.status_code == 401:
        raise ProviderAuthError("GitHub didn't accept that token")
    if response.status_code == 403:
        raise ProviderAuthError(
            "GitHub blocked that token — it may need organization approval, "
            "or GitHub may be rate limiting"
        )
    if response.status_code >= 400:
        raise ProviderAuthError("GitHub couldn't check that token — try again")
    try:
        repos = response.json()
    except ValueError as exc:
        raise ProviderAuthError("GitHub returned an unreadable repository list") from exc
    if not isinstance(repos, list) or not repos:
        raise ProviderAuthError(
            "that token can't see any repositories — select at least one in GitHub's token settings"
        )


async def list_issues(ctx: Any, params: dict[str, Any]) -> Any:
    path = _repo_path(params, "/issues")
    if path is None:
        return _err("a repository is required, like owner/name")
    query: dict[str, Any] = {"state": _state(params)}
    for key in ("sort", "direction", "assignee", "creator", "since"):
        value = str(params.get(key) or "").strip()
        if value:
            query[key] = value
    labels = _string_list(params.get("labels"))
    if labels:
        query["labels"] = ",".join(labels)
    query.update(_paging(params))
    data = await ctx.json("GET", path, params=query, headers=_HEADERS)
    if not isinstance(data, list):
        return _err("GitHub returned an unreadable issue list")
    issues = [
        _issue_view(item)
        for item in data
        if isinstance(item, dict) and not item.get("pull_request")
    ]
    return _ok({"issues": issues, "count": len(issues)})


async def get_issue(ctx: Any, params: dict[str, Any]) -> Any:
    number = _number(params)
    if number is None:
        return _err("a repository and issue number are required")
    path = _repo_path(params, f"/issues/{number}")
    if path is None:
        return _err("a repository and issue number are required")
    data = await ctx.json("GET", path, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned an unreadable issue")
    return _ok({"issue": _issue_view(data)})


async def create_issue(ctx: Any, params: dict[str, Any]) -> Any:
    path = _repo_path(params, "/issues")
    if path is None:
        return _err("a repository is required, like owner/name")
    title = str(params.get("title") or "").strip()
    if not title:
        return _err("an issue title is required")
    body: dict[str, Any] = {"title": title}
    text = str(params.get("body") or "").strip()
    if text:
        body["body"] = text
    labels = _string_list(params.get("labels"))
    if labels:
        body["labels"] = labels
    assignees = _string_list(params.get("assignees"))
    if assignees:
        body["assignees"] = assignees
    milestone = _bounded_int(params.get("milestone"))
    if milestone is not None:
        body["milestone"] = milestone
    data = await ctx.json("POST", path, json=body, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned an unreadable issue")
    return _ok({"issue": _issue_view(data)})


async def comment_on_issue(ctx: Any, params: dict[str, Any]) -> Any:
    number = _number(params)
    if number is None:
        return _err("a repository and issue number are required")
    path = _repo_path(params, f"/issues/{number}/comments")
    if path is None:
        return _err("a repository and issue number are required")
    text = str(params.get("body") or "").strip()
    if not text:
        return _err("a comment is required")
    data = await ctx.json("POST", path, json={"body": text}, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned an unreadable comment")
    return _ok({"comment": _comment_view(data)})


async def list_pull_requests(ctx: Any, params: dict[str, Any]) -> Any:
    path = _repo_path(params, "/pulls")
    if path is None:
        return _err("a repository is required, like owner/name")
    query: dict[str, Any] = {"state": _state(params)}
    for key in ("sort", "direction", "base", "head"):
        value = str(params.get(key) or "").strip()
        if value:
            query[key] = value
    query.update(_paging(params))
    data = await ctx.json("GET", path, params=query, headers=_HEADERS)
    if not isinstance(data, list):
        return _err("GitHub returned an unreadable pull request list")
    pulls = [_pull_view(item) for item in data if isinstance(item, dict)]
    return _ok({"pull_requests": pulls, "count": len(pulls)})


async def get_pull_request(ctx: Any, params: dict[str, Any]) -> Any:
    number = _number(params)
    if number is None:
        return _err("a repository and pull request number are required")
    path = _repo_path(params, f"/pulls/{number}")
    if path is None:
        return _err("a repository and pull request number are required")
    data = await ctx.json("GET", path, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned an unreadable pull request")
    return _ok({"pull_request": _pull_view(data)})


async def comment_on_pull_request(ctx: Any, params: dict[str, Any]) -> Any:
    number = _number(params)
    if number is None:
        return _err("a repository and pull request number are required")
    path = _repo_path(params, f"/issues/{number}/comments")
    if path is None:
        return _err("a repository and pull request number are required")
    text = str(params.get("body") or "").strip()
    if not text:
        return _err("a comment is required")
    data = await ctx.json("POST", path, json={"body": text}, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned an unreadable comment")
    return _ok({"comment": _comment_view(data)})


async def list_notifications(ctx: Any, params: dict[str, Any]) -> Any:
    query: dict[str, Any] = {}
    if params.get("all"):
        query["all"] = "true"
    if params.get("participating"):
        query["participating"] = "true"
    for key in ("since", "before"):
        value = str(params.get(key) or "").strip()
        if value:
            query[key] = value
    query.update(_paging(params))
    data = await ctx.json("GET", "/notifications", params=query, headers=_HEADERS)
    if not isinstance(data, list):
        return _err("GitHub returned an unreadable notification list")
    notifications = [_notification_view(item) for item in data if isinstance(item, dict)]
    return _ok({"notifications": notifications, "count": len(notifications)})


async def search(ctx: Any, params: dict[str, Any]) -> Any:
    query_text = str(params.get("query") or "").strip()
    if not query_text:
        return _err("a search query is required")
    query: dict[str, Any] = {"q": query_text}
    for key in ("sort", "order"):
        value = str(params.get(key) or "").strip()
        if value:
            query[key] = value
    query.update(_paging(params))
    data = await ctx.json("GET", "/search/issues", params=query, headers=_HEADERS)
    if not isinstance(data, dict):
        return _err("GitHub returned unreadable search results")
    items = data.get("items") or []
    if not isinstance(items, list):
        return _err("GitHub returned unreadable search results")
    return _ok(
        {
            "items": [_issue_view(item) for item in items if isinstance(item, dict)],
            "total_count": int(data.get("total_count") or 0),
            "incomplete_results": bool(data.get("incomplete_results")),
        }
    )


_TOOL_LIST_ISSUES = ToolDefinition(
    key="list_issues",
    description=(
        "List GitHub issues in a repository, newest first. Use this for issues, "
        "bugs, or tasks. Returns title, state, labels, assignee, and issue number."
    ),
    effect="read",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Repository in owner/name form.",
            },
            "state": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "description": "Issue state; defaults to open.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only issues with these labels.",
            },
            "assignee": {"type": "string", "description": "GitHub login."},
            "creator": {"type": "string", "description": "GitHub login."},
            "sort": {
                "type": "string",
                "enum": ["created", "updated", "comments"],
                "description": "Sort field.",
            },
            "direction": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort direction.",
            },
            "since": {
                "type": "string",
                "description": "Only issues updated after this ISO 8601 time.",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many issues to return.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "Page number."},
        },
        "required": ["repo"],
    },
    handler=list_issues,
)

_TOOL_GET_ISSUE = ToolDefinition(
    key="get_issue",
    description="Read one GitHub issue by repository and issue number.",
    effect="read",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "number": {"type": "integer", "description": "Issue number."},
        },
        "required": ["repo", "number"],
    },
    handler=get_issue,
)

_TOOL_CREATE_ISSUE = ToolDefinition(
    key="create_issue",
    description="Open a new GitHub issue in a repository.",
    effect="write",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "title": {"type": "string", "description": "Issue title."},
            "body": {"type": "string", "description": "Issue description."},
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Labels to add.",
            },
            "assignees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GitHub logins to assign.",
            },
            "milestone": {
                "type": "integer",
                "description": "Milestone number, when the repository uses them.",
            },
        },
        "required": ["repo", "title"],
    },
    handler=create_issue,
)

_TOOL_COMMENT_ISSUE = ToolDefinition(
    key="comment_on_issue",
    description="Add a comment to a GitHub issue.",
    effect="write",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "number": {"type": "integer", "description": "Issue number."},
            "body": {"type": "string", "description": "Comment text."},
        },
        "required": ["repo", "number", "body"],
    },
    handler=comment_on_issue,
)

_TOOL_LIST_PULL_REQUESTS = ToolDefinition(
    key="list_pull_requests",
    description=(
        "List GitHub pull requests in a repository, newest first. Use this for "
        "PRs and code review requests. Returns title, state, branches, and number."
    ),
    effect="read",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "state": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "description": "Pull request state; defaults to open.",
            },
            "sort": {
                "type": "string",
                "enum": ["created", "updated", "popularity", "long-running"],
                "description": "Sort field.",
            },
            "direction": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort direction.",
            },
            "head": {
                "type": "string",
                "description": "Filter by head branch, like owner:branch.",
            },
            "base": {"type": "string", "description": "Filter by base branch."},
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many pull requests to return.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "Page number."},
        },
        "required": ["repo"],
    },
    handler=list_pull_requests,
)

_TOOL_GET_PULL_REQUEST = ToolDefinition(
    key="get_pull_request",
    description="Read one GitHub pull request by repository and pull request number.",
    effect="read",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "number": {"type": "integer", "description": "Pull request number."},
        },
        "required": ["repo", "number"],
    },
    handler=get_pull_request,
)

_TOOL_COMMENT_PULL_REQUEST = ToolDefinition(
    key="comment_on_pull_request",
    description="Add a comment to a GitHub pull request.",
    effect="write",
    input_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "number": {"type": "integer", "description": "Pull request number."},
            "body": {"type": "string", "description": "Comment text."},
        },
        "required": ["repo", "number", "body"],
    },
    handler=comment_on_pull_request,
)

_TOOL_LIST_NOTIFICATIONS = ToolDefinition(
    key="list_notifications",
    description=(
        "List GitHub notifications for the connected account. This works only "
        "with browser sign-in, not a fine-grained token."
    ),
    effect="read",
    required_scopes=(GITHUB_NOTIFICATIONS,),
    available_in=("oauth",),
    input_schema={
        "type": "object",
        "properties": {
            "all": {
                "type": "boolean",
                "description": "Include notifications already marked read.",
            },
            "participating": {
                "type": "boolean",
                "description": "Only notifications the user is participating in.",
            },
            "since": {
                "type": "string",
                "description": "Only notifications updated after this ISO 8601 time.",
            },
            "before": {
                "type": "string",
                "description": "Only notifications updated before this ISO 8601 time.",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many notifications to return.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "Page number."},
        },
        "required": [],
    },
    handler=list_notifications,
)

_TOOL_SEARCH = ToolDefinition(
    key="search",
    description=(
        "Search GitHub issues and pull requests with GitHub's search syntax. "
        "Returns matching issues and PRs with repository, title, and number."
    ),
    effect="read",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "GitHub search query, like repo:owner/name is:open bug.",
            },
            "sort": {
                "type": "string",
                "enum": ["comments", "reactions", "created", "updated"],
                "description": "Sort field.",
            },
            "order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort direction.",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many results to return.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "Page number."},
        },
        "required": ["query"],
    },
    handler=search,
)

GITHUB_APP = AppDefinition(
    key="github",
    name="GitHub",
    provider="github",
    scopes=(
        GITHUB_READ_USER,
        GITHUB_REPO,
        GITHUB_NOTIFICATIONS,
        GITHUB_READ_ORG,
        GITHUB_OFFLINE,
    ),
    tools=(
        _TOOL_LIST_ISSUES,
        _TOOL_GET_ISSUE,
        _TOOL_CREATE_ISSUE,
        _TOOL_COMMENT_ISSUE,
        _TOOL_LIST_PULL_REQUESTS,
        _TOOL_GET_PULL_REQUEST,
        _TOOL_COMMENT_PULL_REQUEST,
        _TOOL_LIST_NOTIFICATIONS,
        _TOOL_SEARCH,
    ),
)

GITHUB_PROVIDER = ProviderDefinition(
    key="github",
    name="GitHub",
    auth_modes=("token", "oauth"),
    default_auth_mode="token",
    authorization_endpoint="https://github.com/login/oauth/authorize",
    token_endpoint="https://github.com/login/oauth/access_token",
    revocation_endpoint="https://api.github.com/applications/{client_id}/grant",
    userinfo_endpoint="https://api.github.com/user",
    api_base=GITHUB_API,
    client_id_name="GITHUB_CLIENT_ID",
    client_secret_name="GITHUB_CLIENT_SECRET",
    setup_url="https://github.com/settings/developers",
    setup_steps=_GITHUB_SETUP_STEPS,
    static_token=StaticTokenSetup(
        token_label="GitHub fine-grained personal access token",
        token_prefix="github_pat_",
        expires=True,
        instructions=_GITHUB_TOKEN_STEPS,
        permissions=(
            "Metadata: read-only",
            "Issues: read and write",
            "Pull requests: read and write",
        ),
        token_url="https://github.com/settings/tokens?type=beta",
    ),
    validate_token=_validate_token,
    forbidden_plain=(
        "GitHub blocked that action — it may need more access, or an "
        "organization may need to approve the token or OAuth app"
    ),
    not_found_plain="that item isn't there, or this GitHub token can't see it",
    apps=(GITHUB_APP,),
)

PROVIDERS = [GITHUB_PROVIDER]
