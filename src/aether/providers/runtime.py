"""The provider runtime: one place that owns connections, tools, and polls."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..authz.audit import AuditLog
from ..config import AppConfig
from ..connectors.registry import ToolRegistry
from ..llm.types import ToolSpec, with_plain_param
from ..secret_env import EnvResolver
from ..secret_store import SecretStore
from .oauth import OAuthEngine
from .store import ProviderStore
from .types import (
    AppDefinition,
    ConnectResult,
    PollResult,
    ProviderAppView,
    ProviderAuthError,
    ProviderConnection,
    ProviderCryptoError,
    ProviderDefinition,
    ProviderError,
    ProviderHTTPError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderReauthRequired,
)

log = logging.getLogger("aether.providers")

_REFRESH_WINDOW = timedelta(seconds=60)
_RETRY_AFTER_CAP_SECONDS = 2.0


class ProviderContext:
    """What a provider tool handler gets: the connection and safe HTTP."""

    def __init__(
        self,
        runtime: ProviderRuntime,
        provider: ProviderDefinition,
        app: AppDefinition,
        connection: ProviderConnection,
    ) -> None:
        self._runtime = runtime
        self.provider = provider
        self.app = app
        self.connection = connection

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = (
            path if path.startswith(("http://", "https://")) else f"{self.provider.api_base}{path}"
        )
        return await self._runtime.request(self.connection, method, url, **kwargs)

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderHTTPError(
                502, f"{self.provider.name} returned an unreadable answer"
            ) from exc

    async def get_sync(self, key: str) -> str | None:
        return await self._runtime.get_sync(self.provider.key, self.app.key, key)

    async def set_sync(self, key: str, value: str) -> None:
        await self._runtime.set_sync(self.provider.key, self.app.key, key, value)


class ProviderRuntime:
    def __init__(
        self,
        *,
        definitions: list[ProviderDefinition],
        store: ProviderStore | None,
        oauth: OAuthEngine | None,
        http: httpx.AsyncClient,
        registry: ToolRegistry,
        audit: AuditLog,
        resolver: EnvResolver,
        config: AppConfig,
        public_base_url: str,
        port: int = 8000,
        secrets: SecretStore | None = None,
        approvals: Any = None,
    ) -> None:
        self._definitions = {provider.key: provider for provider in definitions}
        self._store = store
        self._oauth = oauth
        self._http = http
        self._registry = registry
        self._audit = audit
        self._resolver = resolver
        self._secrets = secrets
        self._approvals = approvals
        self._config = config
        self._public_base_url = public_base_url.rstrip("/")
        self._port = port
        self._locks: dict[str, asyncio.Lock] = {}
        self._apps: dict[str, tuple[ProviderDefinition, AppDefinition]] = {}
        for provider in definitions:
            for app in provider.apps:
                self._apps[app.key] = (provider, app)

    # -- registration -------------------------------------------------------

    async def register_tools(self) -> int:
        if self._store is None or not self._config.providers.enabled:
            self._registry.drop_provider_tools()
            return 0
        self._registry.drop_provider_tools()
        try:
            connections = {conn.provider: conn for conn in await self._store.list()}
        except Exception:
            log.exception("could not load provider connections")
            return 0
        added = 0
        added_apps: set[str] = set()
        for _app_key, (provider, app) in self._apps.items():
            connection = connections.get(provider.key)
            if connection is None or connection.status != "active":
                continue
            for tool in self._available_tools(connection, app):
                spec = ToolSpec(
                    name=f"{app.key}__{tool.key}",
                    description=tool.description,
                    input_schema=with_plain_param(tool.input_schema),
                    source=app.key,
                )
                self._registry.add_provider(
                    spec,
                    self._handler(provider, app, tool.key),
                    effect=tool.effect,
                    source=app.key,
                    auth_mode=connection.auth_mode,
                )
                added += 1
                added_apps.add(app.key)
        if "gmail" in added_apps:
            # direct Gmail owns the name now; the hub's copy must not appear
            # beside it, or the model has two ways to send the same mail.
            self._registry.suppress_mcp_prefix("composio__GMAIL_")
        return added

    def _handler(self, provider: ProviderDefinition, app: AppDefinition, tool_key: str):
        tool = next(tool for tool in app.tools if tool.key == tool_key)

        async def handler(params: dict[str, Any]) -> Any:
            connection = await self._connection(provider.key)
            ctx = ProviderContext(self, provider, app, connection)
            return await tool.handler(ctx, params or {})

        return handler

    def _available_tools(self, connection: ProviderConnection, app: AppDefinition):
        scopes = set(connection.scopes.split()) if connection.scopes else set()
        for tool in app.tools:
            if connection.auth_mode not in tool.available_in:
                continue
            if tool.required_scopes and not set(tool.required_scopes).issubset(scopes):
                continue
            yield tool

    # -- connection views ---------------------------------------------------

    async def feed(self) -> dict[str, Any]:
        configured = (
            self._store is not None and self._oauth is not None and self._config.providers.enabled
        )
        connections: dict[str, ProviderConnection] = {}
        if self._store is not None:
            try:
                connections = {conn.provider: conn for conn in await self._store.list()}
            except Exception:
                log.exception("could not read provider connections")
        providers: list[dict[str, Any]] = []
        apps: list[ProviderAppView] = []
        for provider in self._definitions.values():
            client_configured = False
            if self._oauth is not None:
                client_configured = await self._oauth.client_configured(provider)
            providers.append(
                {
                    "key": provider.key,
                    "name": provider.name,
                    "configured": client_configured,
                    "auth_modes": list(provider.auth_modes),
                    "default_auth_mode": provider.default_auth_mode,
                    "setup_url": provider.setup_url,
                    "setup_steps": list(provider.setup_steps),
                    "redirect_uri": self.redirect_uri(provider),
                    "client_id_name": provider.client_id_name or "",
                    "client_secret_name": provider.client_secret_name or "",
                }
            )
            connection = connections.get(provider.key)
            for app in provider.apps:
                available = 0
                if connection is not None and connection.status == "active":
                    available = sum(1 for _ in self._available_tools(connection, app))
                setup_required = not configured or (
                    provider.default_auth_mode == "oauth" and not client_configured
                )
                apps.append(
                    ProviderAppView(
                        key=app.key,
                        name=app.name,
                        provider=provider.key,
                        connected=connection is not None,
                        status=connection.status if connection is not None else "not_connected",
                        identity=connection.account_label if connection is not None else "",
                        auth_mode=connection.auth_mode
                        if connection is not None
                        else provider.default_auth_mode,
                        scopes=tuple(connection.scopes.split())
                        if connection is not None and connection.scopes
                        else app.scopes,
                        actions=available,
                        setup_required=setup_required,
                        setup_url=provider.setup_url,
                        setup_steps=provider.setup_steps,
                        redirect_uri=self.redirect_uri(provider),
                        last_error=connection.last_error if connection is not None else "",
                    )
                )
        return {
            "configured": configured,
            "apps": [app.__dict__ for app in apps],
            "providers": providers,
        }

    async def connect(self, app_key: str) -> ConnectResult:
        provider, app = self._app(app_key)
        if self._store is None or self._oauth is None or not self._config.providers.enabled:
            raise ProviderNotConfigured("provider connections are disabled")
        if provider.default_auth_mode == "token":
            setup = provider.static_token
            return ConnectResult(
                app=app.key,
                provider=provider.key,
                token_required=True,
                setup_url=provider.setup_url,
                setup_steps=setup.instructions if setup else provider.setup_steps,
            )
        if not await self._oauth.client_configured(provider):
            return ConnectResult(
                app=app.key,
                provider=provider.key,
                setup_required=True,
                setup_url=provider.setup_url,
                setup_steps=provider.setup_steps,
                redirect_uri=self.redirect_uri(provider),
                client_id_name=provider.client_id_name or "",
                client_secret_name=provider.client_secret_name or "",
            )
        existing = await self._connection_or_none(provider.key)
        existing_scopes = tuple(existing.scopes.split()) if existing and existing.scopes else ()
        prompt_consent = (
            existing is None
            or existing.status != "active"
            or not set(app.scopes).issubset(set(existing_scopes))
        )
        url = await self._oauth.authorization_url(
            provider,
            app,
            redirect_uri=self.redirect_uri(provider),
            existing_scopes=existing_scopes,
            prompt_consent=prompt_consent,
        )
        return ConnectResult(
            app=app.key,
            provider=provider.key,
            redirect_url=url,
            redirect_uri=self.redirect_uri(provider),
            already_connected=existing is not None and existing.status == "active",
        )

    async def save_client(
        self, provider_key: str, client_id: str, client_secret: str
    ) -> dict[str, Any]:
        """Write-only OAuth client setup. The values go to the encrypted
        secret store; the reply names what was stored, never the values."""
        provider = self._provider(provider_key)
        if not provider.client_id_name or not provider.client_secret_name:
            raise ProviderNotConfigured(f"{provider.name} doesn't use an OAuth client")
        if self._secrets is None:
            raise ProviderNotConfigured("the secret store isn't wired")
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        if not client_id or not client_secret:
            raise ProviderAuthError("both the client ID and client secret are needed")
        await self._secrets.set(provider.client_id_name, client_id)
        await self._secrets.set(provider.client_secret_name, client_secret)
        await self._audit.append(
            actor="owner",
            tool_name=f"{provider.key}__client_setup",
            decision="allow",
            rules_matched="builtin:owner-setup",
            params={
                "provider": provider.key,
                "client_id_name": provider.client_id_name,
                "client_secret_name": provider.client_secret_name,
            },
            outcome="OAuth client credentials stored encrypted — values never recorded",
        )
        return await self.setup_recipe(provider_key)

    async def handle_callback(
        self, provider_key: str, *, code: str, state: str
    ) -> ProviderConnection:
        provider = self._provider(provider_key)
        if self._store is None or self._oauth is None or not self._config.providers.enabled:
            raise ProviderNotConfigured("provider connections are disabled")
        connection = await self._oauth.exchange(provider, code=code, state=state)
        await self.register_tools()
        return connection

    async def connect_token(
        self,
        app_key: str,
        token: str,
        *,
        expires_at: datetime | None = None,
    ) -> ProviderConnection:
        provider, app = self._app(app_key)
        if self._store is None or self._oauth is None or not self._config.providers.enabled:
            raise ProviderNotConfigured("provider connections are disabled")
        if "token" not in provider.auth_modes:
            raise ProviderAuthError(f"{provider.name} only supports browser sign-in")
        setup = provider.static_token
        if setup and setup.token_prefix and not token.startswith(setup.token_prefix):
            raise ProviderAuthError(
                f"that isn't a {setup.token_label} — it should start with {setup.token_prefix}"
            )
        account_id, label = await self._oauth.identity(provider, token)
        connection = ProviderConnection(
            provider=provider.key,
            auth_mode="token",
            account_label=label,
            account_id=account_id,
            app=app.key,
            access_token=token,
            scopes="",
            expires_at=expires_at,
            status="active",
            last_refreshed_at=datetime.now(UTC),
        )
        await self._store.upsert(connection)
        await self._audit.append(
            actor="owner",
            tool_name=f"{app.key}__connect",
            decision="allow",
            rules_matched="builtin:token-consent",
            params={"provider": provider.key, "app": app.key, "auth_mode": "token"},
            outcome=f"{provider.name} connected",
        )
        await self.register_tools()
        return connection

    async def disconnect(self, app_key: str) -> None:
        provider, app = self._app(app_key)
        if self._store is None or self._oauth is None or not self._config.providers.enabled:
            raise ProviderNotConfigured("provider connections are disabled")
        connection = await self._connection_or_none(provider.key)
        if connection is not None:
            await self._oauth.revoke(provider, connection.access_token)
            if connection.refresh_token:
                await self._oauth.revoke(provider, connection.refresh_token)
            await self._store.delete(provider.key)
        await self._fail_pending(
            app.key, f"{app.key} was disconnected — the held action did not run"
        )
        self._registry.drop_provider_tools()
        await self._audit.append(
            actor="owner",
            tool_name=f"{app.key}__disconnect",
            decision="allow",
            rules_matched="builtin:owner-disconnect",
            params={"provider": provider.key, "app": app.key},
            outcome=f"{provider.name} disconnected",
        )
        await self.register_tools()

    async def get_sync(self, provider: str, app: str, key: str) -> str | None:
        if self._store is None:
            return None
        return await self._store.get_sync(provider, app, key)

    async def set_sync(self, provider: str, app: str, key: str, value: str) -> None:
        if self._store is None:
            return
        await self._store.set_sync(provider, app, key, value)

    # -- polling ------------------------------------------------------------

    async def poll_due(self, now: datetime | None = None) -> list[PollResult]:
        if self._store is None or self._oauth is None or not self._config.providers.enabled:
            return []
        now = now or datetime.now(UTC)
        try:
            connections = {conn.provider: conn for conn in await self._store.list()}
        except Exception:
            log.exception("could not read provider connections for polling")
            return []
        results: list[PollResult] = []
        for provider in self._definitions.values():
            connection = connections.get(provider.key)
            if connection is None or connection.status != "active":
                continue
            for app in provider.apps:
                poll = app.poll
                if poll is None:
                    continue
                lock = self._locks.setdefault(f"poll:{app.key}", asyncio.Lock())
                async with lock:
                    interval = self._poll_interval(app)
                    if interval <= 0:
                        continue
                    last_raw = await self._store.get_sync(
                        provider.key, app.key, f"poll:{poll.key}:last"
                    )
                    last = _parse_time(last_raw)
                    if last is not None and now < last + timedelta(minutes=interval):
                        continue
                    ctx = ProviderContext(self, provider, app, connection)
                    try:
                        payload = await poll.handler(ctx)
                    except ProviderError as exc:
                        log.warning("provider poll failed for %s: %s", app.key, exc)
                        await self._store.set_sync(
                            provider.key, app.key, f"poll:{poll.key}:last", now.isoformat()
                        )
                        continue
                    await self._store.set_sync(
                        provider.key, app.key, f"poll:{poll.key}:last", now.isoformat()
                    )
                    if payload:
                        results.append(
                            PollResult(app=app.key, tool=f"{app.key}__{poll.key}", payload=payload)
                        )
        return results

    def _poll_interval(self, app: AppDefinition) -> float:
        if app.key == "gmail":
            return self._config.providers.gmail.poll_minutes
        return app.poll.every_minutes if app.poll is not None else 0.0

    # -- HTTP ---------------------------------------------------------------

    async def request(
        self, connection: ProviderConnection, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        provider = self._provider(connection.provider)
        await self._ensure_access(connection)
        response = await self._send(connection, method, url, **kwargs)
        if response.status_code == 401 and connection.auth_mode == "oauth":
            await self._refresh(connection)
            response = await self._send(connection, method, url, **kwargs)
        if response.status_code == 401:
            await self._mark_reauth(connection, "the provider rejected the saved sign-in")
            raise ProviderReauthRequired(f"{provider.name} needs to be reconnected")
        if response.status_code == 429:
            retry_after = _retry_after(response)
            if retry_after is not None:
                await asyncio.sleep(min(retry_after, _RETRY_AFTER_CAP_SECONDS))
                response = await self._send(connection, method, url, **kwargs)
            if response.status_code == 429:
                raise ProviderRateLimited(f"{provider.name} is asking me to slow down")
        if response.status_code >= 500 and method.upper() in ("GET", "HEAD"):
            # a read is safe to retry; a send may have left Aether before
            # the 5xx landed, so its outcome is unknown and it must not run
            # twice. The caller gets the plain failure and the user decides.
            await asyncio.sleep(0.2)
            response = await self._send(connection, method, url, **kwargs)
        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code, _plain_http_error(provider, response.status_code)
            )
        return response

    async def _send(
        self, connection: ProviderConnection, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        provider = self._provider(connection.provider)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault(
            "Authorization", f"{connection.token_type or 'Bearer'} {connection.access_token}"
        )
        headers.setdefault("Accept", "application/json")
        try:
            return await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            if method.upper() not in ("GET", "HEAD"):
                raise ProviderHTTPError(
                    504,
                    f"{provider.name} timed out — I can't tell whether that action "
                    "went through; check before trying again",
                ) from exc
            raise ProviderHTTPError(504, f"{provider.name} isn't responding right now") from exc
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(502, f"{provider.name} isn't responding right now") from exc

    async def _ensure_access(self, connection: ProviderConnection) -> None:
        if connection.auth_mode != "oauth":
            return
        if connection.expires_at is None:
            return
        if connection.expires_at <= datetime.now(UTC) + _REFRESH_WINDOW:
            await self._refresh(connection)

    async def _refresh(self, connection: ProviderConnection) -> None:
        provider = self._provider(connection.provider)
        if self._oauth is None or self._store is None:
            raise ProviderNotConfigured("provider connections are disabled")
        lock = self._locks.setdefault(f"refresh:{provider.key}", asyncio.Lock())
        async with lock:
            fresh = await self._store.get(provider.key)
            if fresh is None:
                raise ProviderReauthRequired(f"{provider.name} needs to be reconnected")
            if (
                fresh.expires_at is not None
                and fresh.expires_at > datetime.now(UTC) + _REFRESH_WINDOW
            ):
                _copy_connection(fresh, connection)
                return
            try:
                refreshed = await self._oauth.refresh(provider, fresh)
            except ProviderError as exc:
                await self._mark_reauth(connection, "the saved sign-in couldn't be refreshed")
                if isinstance(exc, ProviderReauthRequired):
                    raise
                raise ProviderReauthRequired(f"{provider.name} needs to be reconnected") from exc
            _copy_connection(refreshed, connection)

    async def _mark_reauth(self, connection: ProviderConnection, error: str) -> None:
        connection.status = "reauth_required"
        connection.last_error = error
        if self._store is not None:
            await self._store.set_status(connection.provider, "reauth_required", error)
        app_key = connection.app or connection.provider
        try:
            await self._audit.append(
                actor="system",
                tool_name=f"{app_key}__reauth",
                decision="error",
                rules_matched="provider:sign-in",
                params={"provider": connection.provider},
                outcome=error,
            )
        except Exception:
            log.exception("recording the reauth audit row failed — continuing")
        await self._fail_pending(
            app_key, f"{app_key} needs reconnecting — the held action did not run"
        )
        # drop this app's tools but keep any other connected app callable
        try:
            await self.register_tools()
        except Exception:
            log.exception("re-registering provider tools after reauth failed — continuing")

    async def _fail_pending(self, app_key: str, reason: str) -> None:
        if self._approvals is None:
            return
        try:
            await self._approvals.fail_pending_matching(f"{app_key}__", reason)
        except Exception:
            log.exception("failing pending approvals for %s failed — continuing", app_key)

    async def _connection(self, provider: str) -> ProviderConnection:
        connection = await self._connection_or_none(provider)
        if connection is None:
            raise ProviderReauthRequired("that app needs to be connected")
        if connection.status != "active":
            raise ProviderReauthRequired("that app needs to be reconnected")
        return connection

    async def _connection_or_none(self, provider: str) -> ProviderConnection | None:
        if self._store is None:
            return None
        try:
            return await self._store.get(provider)
        except ProviderCryptoError as exc:
            raise ProviderReauthRequired("that app's saved sign-in can't be read") from exc

    # -- helpers ------------------------------------------------------------

    def _provider(self, key: str) -> ProviderDefinition:
        provider = self._definitions.get(key)
        if provider is None:
            raise ProviderNotConfigured(f"unknown provider {key!r}")
        return provider

    def _app(self, key: str) -> tuple[ProviderDefinition, AppDefinition]:
        pair = self._apps.get(key)
        if pair is None:
            raise ProviderNotConfigured(f"unknown app {key!r}")
        return pair

    def redirect_uri(self, provider: ProviderDefinition) -> str:
        base = self._public_base_url or f"http://localhost:{self._port}"
        return f"{base}/api/oauth/{provider.key}/callback"

    async def setup_recipe(self, provider_key: str) -> dict[str, Any]:
        provider = self._provider(provider_key)
        configured = False
        if self._oauth is not None:
            configured = await self._oauth.client_configured(provider)
        return {
            "provider": provider.key,
            "name": provider.name,
            "configured": configured,
            "setup_url": provider.setup_url,
            "setup_steps": list(provider.setup_steps),
            "redirect_uri": self.redirect_uri(provider),
            "client_id_name": provider.client_id_name or "",
            "client_secret_name": provider.client_secret_name or "",
        }

    async def close(self) -> None:
        return None


def _copy_connection(source: ProviderConnection, target: ProviderConnection) -> None:
    for field in (
        "auth_mode",
        "account_label",
        "account_id",
        "access_token",
        "refresh_token",
        "key_id",
        "token_type",
        "scopes",
        "expires_at",
        "refresh_expires_at",
        "status",
        "last_refreshed_at",
        "last_error",
    ):
        setattr(target, field, getattr(source, field))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _plain_http_error(provider: ProviderDefinition, status: int) -> str:
    if status == 401:
        return f"{provider.name} needs to be reconnected"
    if status == 403:
        return f"that action needs more access in {provider.name}"
    if status == 404:
        return "that item isn't there"
    if status == 429:
        return f"{provider.name} is asking me to slow down"
    return "that didn't work"
