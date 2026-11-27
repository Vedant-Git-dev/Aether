"""Admission and request-local service construction for private reads."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..agent import AgentLoop, CaptureRequestBox, SurfaceFanout, register_native_tools
from ..agent.traces import Traces
from ..authz.approvals import Approvals
from ..authz.audit import AuditLog
from ..authz.policy import Policy
from ..config import AppConfig, Settings, load_config
from ..config_store import ConfigManager, ConfigOverrides
from ..connectors.registry import ToolRegistry
from ..llm.base import ProviderError as ModelProviderError
from ..llm.embeddings import GeminiEmbedder
from ..llm.model_list import is_sentinel
from ..media.store import MediaStore
from ..memory.commitments import Commitments
from ..memory.event_rows import ScopedEventRows
from ..memory.events import EventStore
from ..memory.facts import Facts
from ..memory.indexes import MemoryIndex
from ..memory.lexical import LexicalIndex
from ..memory.pipeline import QueryPipeline, retrieval_config_from
from ..memory.rows import ScopedRows
from ..memory.summaries import Summaries
from ..memory.vectors import VectorMemory
from ..memory.views import Views
from ..providers.catalog import PROVIDERS
from ..providers.oauth import OAuthEngine
from ..providers.store import ProviderStore
from ..providers.types import ProviderError
from ..scheduler.jobs import ScheduledAction, Scheduler
from ..secret_store import SecretStore
from ..workspace.store import Workspace
from .errors import VaultProtocolError
from .http import bearer_hash
from .lease_work import LeaseWork
from .runtime import STORAGE_ERRORS, VaultRuntime
from .scope import KeyScope, VaultError
from .service import LeaseService
from .store import VaultStore

_TTL = 30
_SCHEDULE_INTERVAL = 20
_REFRESH_INTERVAL = 60
_LOCAL_SCHEDULE_TOOLS = frozenset(
    {
        "workspace.remember",
        "workspace.rewrite",
        "workspace.promote",
        "workspace.finish_setup",
    }
)
log = logging.getLogger("aether.vault.work")


@dataclass(frozen=True)
class Read:
    use_case: str
    categories: tuple[str, ...]


_WORKSPACE = Read("panel.workspace", ("operations", "workspace"))
_ROUTES = {
    "/api/events": Read("panel.events", ("memory",)),
    "/api/approvals": Read("panel.approvals", ("operations",)),
    "/api/workspace": _WORKSPACE,
    "/api/workspace/daily": _WORKSPACE,
    "/api/workspace/search": _WORKSPACE,
    "/api/profile": _WORKSPACE,
    "/api/tasks": Read("panel.tasks", ("operations",)),
    "/v1/models": Read("panel.models", ("operations",)),
}
_MEDIA = Read("panel.media", ("operations", "media"))
_MEMORY_SEARCH = Read("panel.memory_search", ("memory", "operations"))


def active_refs(service: LeaseService, categories: tuple[str, ...]) -> dict[str, int]:
    refs = {
        cat: ver
        for (cat, ver), state in service.installation.entries.items()
        if cat in categories and state == "active"
    }
    if refs.keys() != set(categories):
        raise VaultProtocolError("category_unavailable")
    return refs


class TransactionPool:
    """Use an existing fenced transaction with stores that take a pool."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def scoped_config(pool, scope: KeyScope, ver: int, config_path: str | None) -> AppConfig:
    config = load_config(config_path)
    manager = ConfigManager(
        ConfigOverrides.scoped(pool, scope, ver=ver), Settings.model_construct(), config
    )
    await manager.bootstrap()
    return config


async def scaffold_workspace(service: LeaseService) -> None:
    versions = active_refs(service, ("workspace",))
    ver = versions["workspace"]
    async with (
        service.work(
            operation="scheduled",
            use_case="workspace.scaffold",
            refs=versions.items(),
            ttl=_TTL,
            writes={("workspace", ver): Workspace.SCAFFOLD_WRITES},
        ) as task,
        task.transaction() as conn,
    ):
        await Workspace.scoped(TransactionPool(conn), task.scope, ver=ver).ensure_scaffold()


class _DeferredSchedule(Exception):
    pass


async def run_schedule(service: LeaseService, action_id: int, config_path: str | None) -> None:
    refs = active_refs(service, ("operations", "workspace"))
    operation_ver = refs["operations"]
    workspace_ver = refs["workspace"]
    try:
        async with (
            service.work(
                operation="scheduled",
                use_case="scheduler.local",
                refs=refs.items(),
                ttl=_TTL,
                writes={("operations", operation_ver): 8, ("workspace", workspace_ver): 4},
            ) as task,
            task.transaction() as conn,
        ):
            pool = TransactionPool(conn)
            audit = AuditLog.scoped(pool, task.scope, ver=operation_ver)
            scheduler = Scheduler.scoped(pool, task.scope, audit, ver=operation_ver)

            async def execute(action: ScheduledAction) -> str:
                config = await scoped_config(pool, task.scope, operation_ver, config_path)
                if not config.workspace.enabled:
                    raise _DeferredSchedule()
                workspace = Workspace.scoped(pool, task.scope, ver=workspace_ver)
                approvals = Approvals.scoped(pool, task.scope, audit, ver=operation_ver)
                traces = Traces.scoped(pool, task.scope, ver=operation_ver)
                surfaces = SurfaceFanout()
                capture = CaptureRequestBox()
                tools = ToolRegistry(local_tools=_LOCAL_SCHEDULE_TOOLS)
                register_native_tools(
                    registry=tools,
                    events=None,
                    entities=None,
                    approvals=approvals,
                    scheduler=scheduler,
                    surfaces=surfaces,
                    capture_box=capture,
                    workspace=workspace,
                    audit=audit,
                    traces=traces,
                )
                tool = tools.get(str(action.payload.get("tool", "")))
                if tool is None or tool.spec.id not in _LOCAL_SCHEDULE_TOOLS:
                    raise _DeferredSchedule()
                agent = AgentLoop(
                    providers=None,
                    tools=tools,
                    policy=Policy(config.authz.rules),
                    approvals=approvals,
                    audit=audit,
                    events=None,
                    salience=None,
                    context=None,
                    scheduler=scheduler,
                    surfaces=surfaces,
                    capture_box=capture,
                    config=config,
                    workspace=workspace,
                    traces=traces,
                )
                return await task.step(lambda: agent.execute_scheduled(action))

            await scheduler.run_pending(action_id, execute)
    except* _DeferredSchedule:
        pass


def _schedule_fence_lost(error: BaseException) -> bool:
    return isinstance(error, VaultProtocolError) and error.code in {"locked", "stale_generation"}


async def watch_schedules(runtime: VaultRuntime, pool, config_path: str | None) -> None:
    while True:
        await asyncio.sleep(_SCHEDULE_INTERVAL)
        try:
            service = runtime.serving()
            for action_id in await Scheduler.pending_ids(pool):
                if runtime.serving() is not service:
                    break
                work = asyncio.create_task(run_schedule(service, action_id, config_path))
                try:
                    await work
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    break
                except Exception as exc:
                    stopped = (
                        exc.subgroup(_schedule_fence_lost) is not None
                        if isinstance(exc, ExceptionGroup)
                        else _schedule_fence_lost(exc)
                    )
                    if service.closed or stopped:
                        break
                    log.warning("a scheduled request remains pending")
        except VaultProtocolError:
            continue
        except Exception:
            log.warning("a scheduled request remains pending")


class _StoredSecrets:
    def __init__(self, store: SecretStore) -> None:
        self.resolve = store.get


async def refresh_provider(service: LeaseService, pool, provider_key: str) -> None:
    provider = next((item for item in PROVIDERS if item.key == provider_key), None)
    if provider is None or not provider.token_endpoint:
        return
    refs = active_refs(service, ("credentials",))
    ver = refs["credentials"]
    async with service.work(
        operation="approved_dispatch",
        use_case="provider.refresh",
        refs=refs.items(),
        writes={("credentials", ver): 3},
        ttl=_TTL,
    ) as task:
        http = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        task.resources.push_async_callback(http.aclose)
        secrets = _StoredSecrets(SecretStore.scoped(pool, task.scope, ver=ver))
        async with task.transaction() as conn:
            store = ProviderStore.scoped(TransactionPool(conn), task.scope, ver=ver)
            oauth = OAuthEngine(store=store, resolver=secrets, http=http, audit=None)
            connection = await store.get(provider_key)
            if (
                connection is None
                or not connection.refresh_token
                or not await oauth.client_configured(provider)
                or not await store.claim_refresh(provider_key)
            ):
                return
        # The attempt is durable. A revocation or crash leaves an ambiguous
        # refresh parked instead of replaying a potentially rotated token.
        async with task.transaction() as conn:
            store = ProviderStore.scoped(TransactionPool(conn), task.scope, ver=ver)
            oauth = OAuthEngine(store=store, resolver=secrets, http=http, audit=None)
            try:
                await oauth.refresh(provider, connection)
            except ProviderError:
                await store.set_status(provider_key, "reauth_required", "Sign-in needs attention.")


async def watch_provider_refresh(runtime: VaultRuntime, pool) -> None:
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        try:
            service = runtime.serving()
            for provider_key in await ProviderStore.refresh_due(pool):
                work = asyncio.create_task(refresh_provider(service, pool, provider_key))
                try:
                    await work
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    break
        except VaultProtocolError:
            continue
        except Exception:
            log.warning("a provider refresh remains pending")


def _embedding_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10.0)


class _SearchEmbedder:
    """The configured processor can receive the admitted panel query only."""

    def __init__(
        self, service: LeaseService, pool, model: str, panel_token_hash: bytes, query: str
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", model):
            raise VaultProtocolError("bad_scope")
        self.model = model
        self.dims = GeminiEmbedder.dims
        self._service = service
        self._pool = pool
        self._panel_token_hash = panel_token_hash
        self._query = query
        self._url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
        self._body = httpx.Request(
            "POST",
            self._url,
            json={
                "content": {"parts": [{"text": query}]},
                "taskType": "RETRIEVAL_QUERY",
                "outputDimensionality": self.dims,
            },
        ).content

    async def embed(self, texts, task="query"):
        if task != "query" or texts != [self._query]:
            raise VaultProtocolError("bad_scope")
        versions = active_refs(self._service, ("credentials",))
        async with self._service.work(
            operation="approved_dispatch",
            use_case="memory.embed_query",
            refs=versions.items(),
            ttl=_TTL,
        ) as dispatch:
            store = SecretStore.scoped(self._pool, dispatch.scope, ver=versions["credentials"])
            key = await dispatch.step(lambda: store.get("GEMINI_API_KEY"))
            if not key:
                return []
            client = await dispatch.resources.enter_async_context(_embedding_http())
            for attempt in range(3):
                async with dispatch.transaction() as conn:
                    await _require_delivery_session(conn, self._panel_token_hash, self._service)
                    response = await client.send(
                        httpx.Request(
                            "POST",
                            self._url,
                            content=self._body,
                            headers={"Content-Type": "application/json", "x-goog-api-key": key},
                        ),
                        follow_redirects=False,
                    )
                if response.is_success:
                    return [response.json()["embedding"]["values"]]
                if response.status_code != 429 or attempt == 2:
                    raise ModelProviderError("embedding request failed")
                await asyncio.sleep(1)


def _require_panel_binding(session, service: LeaseService) -> None:
    if session is None:
        raise VaultProtocolError("unauthorized")
    if session["state"] != "available":
        raise VaultProtocolError("locked")
    item = service.installation
    binding = (session["vault_id"], session["device_id"], session["epoch"], session["generation"])
    if binding != (item.vault_id, item.device_id, item.epoch, item.generation):
        raise VaultProtocolError("stale_generation")


async def _require_delivery_session(conn, token_hash: bytes, service: LeaseService) -> None:
    session = await VaultStore(TransactionPool(conn)).active_session(
        token_hash, "panel", for_delivery=True
    )
    _require_panel_binding(session, service)


async def memory_search(
    service: LeaseService,
    pool,
    reader: LeaseWork,
    config: AppConfig,
    query: str,
    panel_token_hash: bytes,
) -> str:
    versions = active_refs(service, ("memory",))
    ver = versions["memory"]
    async with service.work(
        operation="index", use_case="memory.search_index", refs=versions.items(), ttl=_TTL
    ) as index_task:
        index = MemoryIndex()
        lexical = LexicalIndex(
            hot_chunks=config.memory.hot_chunks, cold_batch=config.memory.cold_batch
        )
        model = config.llm.embedding_model or ""
        provider, separator, specific = model.partition("/")
        embedder = None
        if model and not is_sentinel(model) and (not separator or provider == "gemini"):
            embedder = _SearchEmbedder(
                service, pool, specific if separator else model, panel_token_hash, query.strip()
            )
        vectors = VectorMemory.scoped(pool, index_task.scope, embedder, ver=ver)
        index_task.finalizers.callback(index.clear)
        index_task.finalizers.callback(lexical.clear)
        index_task.finalizers.callback(vectors.clear_cache)
        event_rows = ScopedEventRows(index_task.scope, ver=ver)
        await index_task.step(
            lambda: index.rebuild(pool, event_rows, hot_limit=config.memory.hot_events)
        )
        await index_task.step(
            lambda: lexical.rebuild(pool, ScopedRows(index_task.scope, cat="memory", ver=ver))
        )
        await index_task.step(
            lambda: vectors.load_cache(max_chunks=config.memory.vector_cache_max_chunks)
        )
        reader.scope.require("memory", ver)
        audit = AuditLog.scoped(pool, reader.scope, ver=dict(reader.scope.refs)["operations"])
        views = Views(
            index,
            Commitments.scoped(pool, reader.scope, audit, ver=ver),
            facts=Facts.scoped(pool, reader.scope, ver=ver),
            pool=pool,
            event_rows=event_rows,
            timezone=config.memory.default_timezone,
            cold_batch=config.memory.cold_batch,
            core_facts_chars=config.memory.core_facts_chars,
        )
        pipeline = QueryPipeline(
            lexical=lexical,
            index=index,
            views=views,
            vectors=vectors,
            summaries=Summaries.scoped(pool, reader.scope, ver=ver),
            cfg=retrieval_config_from(config.memory),
            timezone=config.memory.default_timezone,
        )
        return await reader.step(lambda: index_task.step(lambda: pipeline.search_text(query)))


class PrivateRoutes:
    def __init__(self, app: ASGIApp, *, config_path: str | None) -> None:
        self.app = app
        self.config_path = config_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket" and scope["path"] == "/ws":
            from .panel_ingress import panel_socket

            await panel_socket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/api/v1/vault/") or not (
            path.startswith("/api/") or path.startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return
        await self._private(scope, receive, send)

    async def _private(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request({**scope, "state": dict(scope.get("state", {}))}, receive)
        sent = False

        async def track(message: Message) -> None:
            nonlocal sent
            if message["type"] == "http.response.start":
                sent = True
            await send(message)

        try:
            token = bearer_hash(request)
            runtime: VaultRuntime | None = getattr(request.app.state, "vault_runtime", None)
            if runtime is None:
                raise VaultProtocolError("unavailable")
            session = await runtime.store.active_session(token, "panel")
            if session is None:
                raise VaultProtocolError("unauthorized")
            if session["state"] != "available":
                raise VaultProtocolError("locked")
            service = runtime.serving()
            origins = request.headers.getlist("origin")
            if origins and origins != [service.installation.origin]:
                raise VaultProtocolError("unauthorized")
            _require_panel_binding(session, service)

            read = _ROUTES.get(request.url.path) if request.method == "GET" else None
            if request.method == "POST" and request.url.path == "/api/memory/search":
                read = _MEMORY_SEARCH
            if request.method == "GET" and request.url.path.startswith("/api/media/"):
                read = _MEDIA
            if read is None:
                raise VaultProtocolError("category_unavailable")

            async def serve() -> None:
                await self._read(request, service, read, token, receive, track)

            worker = asyncio.create_task(serve())
            try:
                await worker
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                raise VaultProtocolError("locked") from None
        except VaultProtocolError as exc:
            if sent:
                raise
            await JSONResponse(
                exc.payload(), status_code=exc.status, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
        except STORAGE_ERRORS:
            if sent:
                raise
            await JSONResponse(
                {"code": "unavailable"}, status_code=503, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)

    async def _read(
        self,
        request: Request,
        service: LeaseService,
        read: Read,
        token: bytes,
        receive: Receive,
        send: Send,
    ) -> None:
        pool = request.app.state.pool
        refs = active_refs(service, read.categories)
        try:
            async with service.work(
                operation="panel_read", use_case=read.use_case, refs=refs.items(), ttl=_TTL
            ) as task:
                request.state.vault_owner = True
                if "operations" in refs:
                    config = await task.step(
                        lambda: scoped_config(
                            pool, task.scope, refs["operations"], self.config_path
                        )
                    )
                else:
                    config = AppConfig()

                if request.url.path == "/api/workspace" and config.workspace.enabled:
                    workspace = Workspace.scoped(pool, task.scope, ver=refs["workspace"])
                    if await task.step(workspace.scaffold_pending):
                        await task.step(lambda: scaffold_workspace(service))

                async def guarded(message: Message) -> None:
                    async with task.transaction() as conn:
                        await _require_delivery_session(conn, token, service)
                        if message["type"] == "http.response.start":
                            message["headers"] = [
                                (k, v)
                                for k, v in message["headers"]
                                if k not in {b"etag", b"cache-control"}
                            ] + [(b"cache-control", b"no-store")]
                        await send(message)

                endpoint = self._endpoint(request, service, task, config, refs, token)
                await endpoint(receive, guarded)
        except* (VaultError, VaultProtocolError, *STORAGE_ERRORS) as exc:
            for error in exc.exceptions:
                if isinstance(error, VaultProtocolError):
                    raise error from None
            raise VaultProtocolError("category_unavailable") from None
        finally:
            request.scope.pop("state", None)

    def _endpoint(
        self,
        request: Request,
        service: LeaseService,
        task: LeaseWork,
        config: AppConfig,
        refs,
        token: bytes,
    ) -> Callable[[Receive, Send], Awaitable[None]]:
        pool = request.app.state.pool
        scope = task.scope
        if request.url.path == "/v1/models":
            llm = config.llm
            ids = {llm.model} | {
                model
                for model in (llm.image_model, llm.audio_model, llm.video_model, llm.salience_model)
                if model and not is_sentinel(model)
            }
            return lambda receive, send: JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "owned_by": llm.provider}
                        for model in sorted(ids)
                    ],
                }
            )(request.scope, receive, send)
        if request.url.path == "/api/memory/search":

            async def search(receive: Receive, send: Send) -> None:
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    raise VaultProtocolError("malformed") from None
                if not isinstance(body, dict) or not isinstance(body.get("query"), str):
                    raise VaultProtocolError("bad_field")
                result = await memory_search(service, pool, task, config, body["query"], token)
                await JSONResponse({"result": result})(request.scope, receive, send)

            return search
        if request.url.path == "/api/events":
            request.state.events = EventStore.scoped(
                pool, scope, None, config.contacts, ver=refs["memory"]
            )
        if request.url.path == "/api/approvals":
            ver = refs["operations"]
            request.state.approvals = Approvals.scoped(
                pool, scope, AuditLog.scoped(pool, scope, ver=ver), ver=ver
            )
        if request.url.path == "/api/tasks":
            ver = refs["operations"]
            request.state.scheduler = Scheduler.scoped(
                pool, scope, AuditLog.scoped(pool, scope, ver=ver), ver=ver
            )
        if "workspace" in refs and config.workspace.enabled:
            request.state.workspace = Workspace.scoped(pool, scope, ver=refs["workspace"])
        if "media" in refs:
            request.state.media_store = MediaStore.scoped(
                pool, scope, config.media, ver=refs["media"]
            )
        return lambda receive, send: self.app(request.scope, receive, send)
