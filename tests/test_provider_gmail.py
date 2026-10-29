"""Gmail handlers: MIME in, readable mail out, history polling."""

from __future__ import annotations

import base64
import json
from email import message_from_bytes

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.google import (
    GMAIL_COMPOSE,
    GMAIL_READ,
    GOOGLE_PROVIDER,
    _plain_text,
    _raw_message,
    create_draft,
    get_message,
    list_messages,
    poll_new_mail,
    send_draft,
    send_email,
)
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import ProviderConnection, ProviderHTTPError
from aether.secret_env import EnvResolver


class FakeCtx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.sync: dict[str, str] = {}
        self.history = {
            "historyId": "102",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "m1"}},
                        {"message": {"id": "m2"}},
                    ]
                }
            ],
        }

    async def json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/profile"):
            return {"historyId": "100", "emailAddress": "me@example.com"}
        if path.endswith("/history"):
            return self.history
        if path.endswith("/messages"):
            return {"messages": [{"id": "m1"}], "nextPageToken": "next"}
        if path.endswith("/messages/send"):
            return {"id": "sent-1", "threadId": "t-sent", "labelIds": ["SENT"]}
        if path.endswith("/drafts/send"):
            return {"id": "sent-draft", "threadId": "t-draft"}
        if path.endswith("/drafts"):
            return {"id": "draft-1", "message": {"id": "m-draft", "threadId": "t-draft"}}
        if path.endswith("/messages/m1"):
            return {
                "id": "m1",
                "threadId": "t1",
                "snippet": "hello",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "Ada <ada@example.com>"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Subject", "value": "Hi"},
                        {"name": "Date", "value": "today"},
                    ],
                    "body": {"data": base64.urlsafe_b64encode(b"hello body").decode()},
                },
            }
        if path.endswith("/messages/m2"):
            return {
                "id": "m2",
                "threadId": "t2",
                "snippet": "second",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Bob <bob@example.com>"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Subject", "value": "Two"},
                    ]
                },
            }
        raise AssertionError(path)

    async def get_sync(self, key: str):
        return self.sync.get(key)

    async def set_sync(self, key: str, value: str) -> None:
        self.sync[key] = value


def test_raw_message_is_valid_mime() -> None:
    raw = _raw_message(
        {
            "to": "ada@example.com",
            "cc": "bob@example.com",
            "subject": "Hello",
            "body": "line one\nline two",
        }
    )
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert parsed["To"] == "ada@example.com"
    assert parsed["Cc"] == "bob@example.com"
    assert parsed["Subject"] == "Hello"
    assert "line one\nline two" in parsed.get_payload(decode=True).decode()


def test_plain_text_prefers_plain_and_unwraps_html() -> None:
    plain = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(b"<b>html</b>").decode()},
            },
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(b"plain body").decode()},
            },
        ],
    }
    assert _plain_text(plain) == "plain body"
    only_html = {
        "mimeType": "text/html",
        "body": {"data": base64.urlsafe_b64encode(b"<p>hello <b>there</b></p>").decode()},
    }
    assert _plain_text(only_html) == "hello there"


@pytest.mark.asyncio
async def test_list_messages_returns_summaries_and_a_page_token() -> None:
    result = await list_messages(FakeCtx(), {"query": "from:ada", "max_results": 5})
    assert result["successful"] is True
    assert result["data"]["next_page_token"] == "next"
    message = result["data"]["messages"][0]
    assert message["id"] == "m1"
    assert message["sender"] == "Ada <ada@example.com>"
    assert message["subject"] == "Hi"


@pytest.mark.asyncio
async def test_history_poll_sets_a_cursor_then_returns_new_mail() -> None:
    ctx = FakeCtx()
    assert await poll_new_mail(ctx) is None
    assert ctx.sync["history_id"] == "100"
    result = await poll_new_mail(ctx)
    assert result["successful"] is True
    assert [m["id"] for m in result["data"]["messages"]] == ["m1", "m2"]
    assert ctx.sync["history_id"] == "102"


@pytest.mark.asyncio
async def test_an_expired_history_cursor_resets_and_returns_recent_mail() -> None:
    class ExpiredCtx(FakeCtx):
        async def json(self, method: str, path: str, **kwargs):
            if path.endswith("/history"):
                raise ProviderHTTPError(404, "history expired")
            return await super().json(method, path, **kwargs)

    ctx = ExpiredCtx()
    ctx.sync["history_id"] = "50"
    result = await poll_new_mail(ctx)
    assert result["successful"] is True
    assert [m["id"] for m in result["data"]["messages"]] == ["m1"]
    assert ctx.sync["history_id"] == "100"


@pytest.mark.asyncio
async def test_get_message_returns_the_plain_body_and_labels() -> None:
    result = await get_message(FakeCtx(), {"message_id": "m1"})
    assert result["successful"] is True
    message = result["data"]["messages"][0]
    assert message["body"] == "hello body"
    assert message["label_ids"] == ["INBOX"]


@pytest.mark.asyncio
async def test_a_missing_id_is_an_error_not_a_success() -> None:
    assert (await get_message(FakeCtx(), {}))["successful"] is False
    assert (await send_draft(FakeCtx(), {}))["successful"] is False


@pytest.mark.asyncio
async def test_send_email_and_draft_tools_return_the_ids() -> None:
    ctx = FakeCtx()
    sent = await send_email(
        ctx,
        {"to": "ada@example.com", "subject": "Hello", "body": "one line"},
    )
    assert sent["data"]["id"] == "sent-1"
    assert sent["data"]["thread_id"] == "t-sent"

    draft = await create_draft(
        ctx,
        {"to": "ada@example.com", "subject": "Draft", "body": "later"},
    )
    assert draft["data"]["id"] == "draft-1"
    assert draft["data"]["message"]["id"] == "m-draft"

    sent_draft = await send_draft(ctx, {"draft_id": "draft-1"})
    assert sent_draft["data"]["id"] == "sent-draft"


class _RuntimeStore:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection

    async def list(self):
        return [self.connection]

    async def get(self, provider: str):
        return self.connection if provider == "google" else None

    async def get_sync(self, provider: str, app: str, key: str):
        return None

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        return None


class _RuntimeOAuth:
    async def client_configured(self, provider) -> bool:
        return True


@pytest.mark.asyncio
async def test_the_gmail_tool_runs_through_the_runtime_with_a_bearer_token() -> None:
    connection = ProviderConnection(
        provider="google",
        auth_mode="oauth",
        app="gmail",
        access_token="access",
        scopes=f"{GMAIL_READ} {GMAIL_COMPOSE}",
        status="active",
    )
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/m1":
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "threadId": "t1",
                    "snippet": "hello",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Ada <ada@example.com>"},
                            {"name": "Subject", "value": "Hi"},
                        ]
                    },
                },
            )
        return httpx.Response(404)

    registry = ToolRegistry()
    runtime = ProviderRuntime(
        definitions=[GOOGLE_PROVIDER],
        store=_RuntimeStore(connection),  # type: ignore[arg-type]
        oauth=_RuntimeOAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        registry=registry,
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver("/nonexistent"),
        config=AppConfig(),
        public_base_url="https://aether.test",
    )
    assert await runtime.register_tools() == 6
    result = json.loads(await registry.execute("gmail__list_messages", {"max_results": 1}))
    assert result["data"]["messages"][0]["subject"] == "Hi"
    assert seen[0] == ("GET", "/gmail/v1/users/me/messages", "Bearer access")
