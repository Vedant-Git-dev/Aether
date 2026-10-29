"""Drive and Docs handlers, shared Google scopes, and picker setup."""

from __future__ import annotations

import json

import httpx
import pytest
from fakes import FakeAudit

from aether.config import AppConfig
from aether.connectors.registry import ToolRegistry
from aether.providers.google import (
    DOCS_APP,
    DRIVE_APP,
    DRIVE_FILE,
    GMAIL_COMPOSE,
    GMAIL_READ,
    GOOGLE_PROVIDER,
    append_text,
    create_document,
    create_folder,
    get_file,
    list_files,
    read_document,
)
from aether.providers.runtime import ProviderRuntime
from aether.providers.types import ProviderAuthError, ProviderConnection
from aether.secret_env import EnvResolver


class FakeCtx:
    def __init__(self, selected: str | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.selected = selected

    async def get_sync(self, key: str):
        return self.selected if key == "picker:selected" else None

    async def json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/files":
            return {
                "files": [
                    {
                        "id": "file-1",
                        "name": "Project notes",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-10-29T10:00:00Z",
                        "createdTime": "2026-10-28T10:00:00Z",
                        "webViewLink": "https://docs.google.com/document/d/file-1/edit",
                        "owners": [{"emailAddress": "me@example.com"}],
                    }
                ],
                "nextPageToken": "next",
            }
        if method == "GET" and path in ("/files/file-1", "/files/file%201"):
            return {
                "id": "file-1",
                "name": "Project notes",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-10-29T10:00:00Z",
                "owners": [{"displayName": "Me"}],
            }
        if method == "POST" and path == "/files":
            return {**kwargs["json"], "id": "folder-1"}
        if method == "POST" and path == "/documents":
            return {
                "documentId": "doc-1",
                "title": kwargs["json"]["title"],
                "revisionId": "rev-1",
            }
        if method == "POST" and path == "/documents/doc-1:batchUpdate":
            return {"replies": [{}]}
        if method == "GET" and path == "/documents/doc-1":
            return {
                "documentId": "doc-1",
                "title": "Project notes",
                "revisionId": "rev-2",
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": "First line\n"}},
                                    {"textRun": {"content": "Second line\n"}},
                                ]
                            }
                        },
                        {
                            "table": {
                                "tableRows": [
                                    {
                                        "tableCells": [
                                            {
                                                "content": [
                                                    {
                                                        "paragraph": {
                                                            "elements": [
                                                                {
                                                                    "textRun": {
                                                                        "content": "Cell text"
                                                                    }
                                                                }
                                                            ]
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        },
                    ]
                },
            }
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.mark.asyncio
async def test_list_files_uses_the_file_scope_and_normalizes_metadata() -> None:
    ctx = FakeCtx(json.dumps([{"id": "file-1", "name": "Project notes"}]))
    result = await list_files(
        ctx,
        {
            "query": "name contains 'notes'",
            "page_size": 500,
            "page_token": "page-2",
            "order_by": "name",
        },
    )
    assert result["successful"] is True
    file = result["data"]["files"][0]
    assert file["id"] == "file-1"
    assert file["name"] == "Project notes"
    assert file["mime_type"] == "application/vnd.google-apps.document"
    assert file["owners"] == ["me@example.com"]
    assert file["selected"] is True
    assert result["data"]["selected"][0]["name"] == "Project notes"
    assert result["data"]["next_page_token"] == "next"

    method, path, kwargs = ctx.calls[0]
    assert (method, path) == ("GET", "/files")
    params = kwargs["params"]
    assert params["pageSize"] == 100
    assert params["q"] == "name contains 'notes'"
    assert params["pageToken"] == "page-2"
    assert params["orderBy"] == "name"
    assert params["spaces"] == "drive"


@pytest.mark.asyncio
async def test_list_files_defaults_to_untrashed_files() -> None:
    ctx = FakeCtx()
    await list_files(ctx, {})
    assert ctx.calls[0][2]["params"]["q"] == "trashed = false"
    assert ctx.calls[0][2]["params"]["pageSize"] == 25


@pytest.mark.asyncio
async def test_get_file_and_create_folder_use_the_expected_paths() -> None:
    ctx = FakeCtx()
    result = await get_file(ctx, {"file_id": "file 1"})
    assert result["successful"] is True
    assert result["data"]["file"]["owners"] == ["Me"]
    assert ctx.calls[0][:2] == ("GET", "/files/file%201")

    result = await create_folder(ctx, {"name": "Aether notes", "parent_id": "parent-1"})
    assert result["successful"] is True
    assert result["data"]["folder"]["id"] == "folder-1"
    assert ctx.calls[-1][2]["json"] == {
        "name": "Aether notes",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["parent-1"],
    }


@pytest.mark.asyncio
async def test_read_document_flattens_paragraphs_and_table_cells() -> None:
    ctx = FakeCtx()
    result = await read_document(ctx, {"document_id": "doc-1"})
    assert result["successful"] is True
    document = result["data"]["document"]
    assert document["title"] == "Project notes"
    assert document["text"] == "First line\nSecond line\nCell text"
    assert document["text_length"] == len(document["text"])
    assert document["url"] == "https://docs.google.com/document/d/doc-1/edit"
    assert ctx.calls[0][:2] == ("GET", "/documents/doc-1")


@pytest.mark.asyncio
async def test_append_text_uses_the_end_of_the_document() -> None:
    ctx = FakeCtx()
    result = await append_text(ctx, {"document_id": "doc-1", "text": "More notes\n"})
    assert result["successful"] is True
    assert result["data"]["appended_characters"] == 11
    assert ctx.calls[0][:2] == ("POST", "/documents/doc-1:batchUpdate")
    assert ctx.calls[0][2]["json"] == {
        "requests": [{"insertText": {"text": "More notes\n", "endOfSegmentLocation": {}}}]
    }


@pytest.mark.asyncio
async def test_create_document_can_add_starting_text() -> None:
    ctx = FakeCtx()
    result = await create_document(ctx, {"title": "Plan", "text": "Start here\n"})
    assert result["successful"] is True
    assert result["data"]["document"]["id"] == "doc-1"
    assert result["data"]["appended_characters"] == 11
    assert ctx.calls[0][:2] == ("POST", "/documents")
    assert ctx.calls[1][:2] == ("POST", "/documents/doc-1:batchUpdate")


@pytest.mark.asyncio
async def test_missing_required_fields_are_errors_not_successes() -> None:
    assert (await get_file(FakeCtx(), {}))["successful"] is False
    assert (await create_folder(FakeCtx(), {}))["successful"] is False
    assert (await create_document(FakeCtx(), {}))["successful"] is False
    assert (await read_document(FakeCtx(), {}))["successful"] is False
    assert (await append_text(FakeCtx(), {"document_id": "doc-1"}))["successful"] is False


class _Store:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.sync: dict[tuple[str, str, str], str] = {}

    async def list(self):
        return [self.connection]

    async def get(self, provider: str):
        return self.connection if provider == "google" else None

    async def get_sync(self, provider: str, app: str, key: str):
        return self.sync.get((provider, app, key))

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        self.sync[(provider, app, key)] = value


class _OAuth:
    async def client_configured(self, provider) -> bool:
        return True

    async def client(self, provider):
        return "client-id.apps.googleusercontent.com", "client-secret"


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value

    async def get(self, name: str):
        return self.values.get(name)

    async def names(self) -> set[str]:
        return set(self.values)


def _connection(scopes: tuple[str, ...]) -> ProviderConnection:
    return ProviderConnection(
        provider="google",
        auth_mode="oauth",
        app="drive",
        access_token="access",
        scopes=" ".join(scopes),
        status="active",
    )


def _runtime(
    connection: ProviderConnection,
    registry: ToolRegistry,
    *,
    handler=None,
    secrets: _Secrets | None = None,
) -> tuple[ProviderRuntime, _Store]:
    store = _Store(connection)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json={"files": []}))
        )
    )
    runtime = ProviderRuntime(
        definitions=[GOOGLE_PROVIDER],
        store=store,  # type: ignore[arg-type]
        oauth=_OAuth(),  # type: ignore[arg-type]
        http=client,
        registry=registry,
        audit=FakeAudit(),  # type: ignore[arg-type]
        resolver=EnvResolver(env_file="/nonexistent", secrets=secrets),  # type: ignore[arg-type]
        config=AppConfig(),
        public_base_url="https://aether.test",
        secrets=secrets,  # type: ignore[arg-type]
    )
    return runtime, store


@pytest.mark.asyncio
async def test_drive_file_scope_registers_drive_and_docs_tools() -> None:
    registry = ToolRegistry()
    runtime, _store = _runtime(_connection((DRIVE_FILE,)), registry)
    assert await runtime.register_tools() == 6
    assert registry.get("drive__list_files") is not None
    assert registry.get("docs__read_document") is not None
    assert registry.get("gmail__list_messages") is None


@pytest.mark.asyncio
async def test_gmail_scopes_do_not_register_drive_or_docs() -> None:
    registry = ToolRegistry()
    runtime, _store = _runtime(_connection((GMAIL_READ, GMAIL_COMPOSE)), registry)
    assert await runtime.register_tools() == 6
    assert registry.get("drive__list_files") is None
    assert registry.get("docs__read_document") is None


@pytest.mark.asyncio
async def test_drive_and_docs_use_their_own_api_bases() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.startswith("/drive/v3/files"):
            return httpx.Response(200, json={"files": []})
        return httpx.Response(
            200,
            json={
                "documentId": "doc-1",
                "title": "Notes",
                "revisionId": "rev-1",
                "body": {"content": []},
            },
        )

    registry = ToolRegistry()
    runtime, _store = _runtime(_connection((DRIVE_FILE,)), registry, handler=handler)
    await runtime.register_tools()
    assert (await registry.execute("drive__list_files", {})) is not None
    assert (await registry.execute("docs__read_document", {"document_id": "doc-1"})) is not None
    assert seen[0].startswith("https://www.googleapis.com/drive/v3/files")
    assert seen[1].startswith("https://docs.googleapis.com/v1/documents/doc-1")


@pytest.mark.asyncio
async def test_picker_recipe_starts_unconfigured_then_saves_public_values() -> None:
    secrets = _Secrets()
    registry = ToolRegistry()
    runtime, _store = _runtime(_connection((DRIVE_FILE,)), registry, secrets=secrets)

    recipe = await runtime.picker_recipe("google")
    assert recipe["configured"] is False
    assert recipe["client_id"] == "client-id.apps.googleusercontent.com"
    assert recipe["scope"] == DRIVE_FILE

    saved = await runtime.save_picker("google", "api-key", "123456")
    assert saved["configured"] is True
    assert saved["api_key"] == "api-key"
    assert saved["app_id"] == "123456"
    assert secrets.values == {
        "GOOGLE_PICKER_API_KEY": "api-key",
        "GOOGLE_PICKER_APP_ID": "123456",
    }


@pytest.mark.asyncio
async def test_record_picker_selection_verifies_and_remembers_the_files() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "doc-1",
                "name": "Project notes",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-10-29T10:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
            },
        )

    registry = ToolRegistry()
    runtime, store = _runtime(_connection((DRIVE_FILE,)), registry, handler=handler)
    result = await runtime.record_picker_selection(
        "drive", [{"id": "doc-1", "name": "Project notes"}]
    )
    assert result["selected"][0]["id"] == "doc-1"
    assert result["count"] == 1
    assert seen and seen[0].startswith("https://www.googleapis.com/drive/v3/files/doc-1")
    saved = json.loads(store.sync[("google", "drive", "picker:selected")])
    assert saved[0]["name"] == "Project notes"


@pytest.mark.asyncio
async def test_picker_selection_requires_the_drive_scope_and_app() -> None:
    registry = ToolRegistry()
    runtime, _store = _runtime(_connection((GMAIL_READ,)), registry)
    with pytest.raises(ProviderAuthError):
        await runtime.record_picker_selection("drive", [{"id": "doc-1"}])
    with pytest.raises(ProviderAuthError):
        await runtime.record_picker_selection("gmail", [{"id": "doc-1"}])


def test_drive_and_docs_declare_only_the_file_scope() -> None:
    assert DRIVE_APP.scopes == (DRIVE_FILE,)
    assert DOCS_APP.scopes == (DRIVE_FILE,)
