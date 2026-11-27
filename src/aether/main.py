"""FastAPI app factory + startup wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, restart
from .api import router as api_router
from .chat.ws import router as chat_router
from .config import BootstrapSettings, Settings
from .db_resilience import CircuitBreaker
from .logging_setup import configure_logging
from .memory.crypto import Cipher, CryptoError, generate_key_b64
from .memory.db import create_pool, run_migrations
from .memory.storage import StorageGuardError, enforce_schema
from .vault.discord_ingress import watch_discord
from .vault.errors import VaultProtocolError
from .vault.http import router as vault_router
from .vault.ingress import Ingress, sealed_destination, watch_ingress
from .vault.keyed_work import PrivateRoutes, watch_provider_refresh, watch_schedules
from .vault.runtime import VaultRuntime
from .vault.schema import KEY_BYTES, b64u_decode
from .vault.signing import RequestSigner
from .vault.store import VaultStore
from .vault.telegram_ingress import watch_telegram

log = logging.getLogger("aether.main")

WEB_DIR = Path(__file__).parent / "web"


class StartupError(RuntimeError):
    """Fatal misconfiguration — boot refuses to continue."""


def _build_cipher(settings: Settings) -> Cipher:
    if settings.encryption_key:
        try:
            return Cipher.from_b64(
                settings.encryption_key,
                key_id=settings.encryption_key_id or "k1",
                previous_b64=settings.encryption_key_previous,
                previous_id=settings.encryption_key_previous_id,
            )
        except CryptoError as exc:
            raise StartupError(f"AETHER_ENCRYPTION_KEY is invalid: {exc}") from exc
    if settings.dev_ephemeral_key:
        log.warning(
            "AETHER_ENCRYPTION_KEY not set — using an EPHEMERAL key. "
            "Encrypted data will be unreadable after a restart. "
            "Set AETHER_DEV_EPHEMERAL_KEY=0 and configure a real key for anything real."
        )
        return Cipher.from_b64(generate_key_b64(), key_id="dev")
    raise StartupError(
        "AETHER_ENCRYPTION_KEY is not set. Generate one with:\n"
        '  python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"\n'
        "(or set AETHER_DEV_EPHEMERAL_KEY=1 for a throwaway dev instance)"
    )


def create_app(
    settings: Settings | BootstrapSettings | None = None,
    config_path: str | None = None,
) -> FastAPI:
    configure_logging()
    if settings is None:
        bootstrap = BootstrapSettings()
    else:
        bootstrap = BootstrapSettings(
            database_url=settings.database_url,
            vault_request_signing_key=settings.vault_request_signing_key,
            vault_pending_wait_seconds=settings.vault_pending_wait_seconds,
            vault_ingress_quota_bytes=settings.vault_ingress_quota_bytes,
            vault_ingress_identity_key=settings.vault_ingress_identity_key,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_ingress_owners=settings.telegram_ingress_owners,
            discord_bot_token=settings.discord_bot_token,
            discord_ingress_owners=settings.discord_ingress_owners,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not bootstrap.database_url:
            raise StartupError("DATABASE_URL is required.")
        try:
            signer = (
                RequestSigner.from_scalar(bootstrap.vault_request_signing_key)
                if bootstrap.vault_request_signing_key
                else None
            )
        except ValueError:
            raise StartupError("AETHER_VAULT_REQUEST_SIGNING_KEY is invalid.") from None

        telegram_enabled = bootstrap.telegram_bot_token and bootstrap.telegram_ingress_owners
        discord_enabled = bootstrap.discord_bot_token and bootstrap.discord_ingress_owners
        identity_key = None
        if telegram_enabled or discord_enabled:
            try:
                identity_key = b64u_decode(
                    bootstrap.vault_ingress_identity_key, field="ingress identity", length=KEY_BYTES
                )
            except VaultProtocolError:
                raise StartupError(
                    "AETHER_VAULT_INGRESS_IDENTITY_KEY must be 32 random bytes in unpadded base64url"
                    " when Telegram or Discord ingress is enabled."
                ) from None

        pool = await create_pool(bootstrap.database_url, breaker=CircuitBreaker())
        try:
            await run_migrations(pool)
            await enforce_schema(pool, "enforce")
            ingress = Ingress(
                pool,
                quota_bytes=bootstrap.vault_ingress_quota_bytes,
                apply=sealed_destination(config_path),
            )
            provider_workers = []
            if telegram_enabled:
                provider_workers.append(
                    partial(
                        watch_telegram,
                        ingress,
                        bootstrap.telegram_bot_token,
                        bootstrap.telegram_ingress_owners,
                    )
                )
            if discord_enabled:
                provider_workers.append(
                    partial(
                        watch_discord,
                        ingress,
                        bootstrap.discord_bot_token,
                        bootstrap.discord_ingress_owners,
                    )
                )
            async with VaultRuntime(
                VaultStore(pool),
                signer,
                pending_wait=bootstrap.vault_pending_wait_seconds,
                leader_workers=tuple(provider_workers),
                ingress_identity_key=identity_key,
            ) as runtime:
                app.state.pool = pool
                app.state.vault_runtime = runtime
                app.state.ingress = ingress
                schedules = asyncio.create_task(watch_schedules(runtime, pool, config_path))
                refreshes = asyncio.create_task(watch_provider_refresh(runtime, pool))
                drain = asyncio.create_task(watch_ingress(runtime, ingress))
                workers = (schedules, refreshes, drain)
                try:
                    yield
                finally:
                    for worker in workers:
                        worker.cancel()
                    for worker in workers:
                        with suppress(asyncio.CancelledError):
                            await worker
        except StorageGuardError as exc:
            raise StartupError(str(exc)) from exc
        finally:
            await pool.close()

    app = FastAPI(title="Aether", version=__version__, lifespan=lifespan)
    app.add_middleware(PrivateRoutes, config_path=config_path)
    app.include_router(api_router)
    app.include_router(chat_router)
    app.include_router(vault_router)
    # the panel is a Vite build artifact (source lives in webapp/) — serve it
    # when present, and say so plainly when it isn't, so a fresh checkout
    # boots without the mount crashing on a directory it doesn't have
    panel_built = (WEB_DIR / "index.html").is_file()
    if panel_built:
        app.mount("/assets", StaticFiles(directory=WEB_DIR), name="web-assets")

    @app.get("/healthz")
    @app.get("/health")  # some pingers default here; same plain 200
    async def healthz() -> dict:
        """Plain 200 — this is the endpoint the external uptime pinger hits."""
        return {"ok": True}

    @app.get("/")
    async def index() -> Response:
        if not panel_built:
            return PlainTextResponse(
                "panel not built — run: cd webapp && npm install && npm run build",
                status_code=503,
            )
        return FileResponse(WEB_DIR / "index.html")

    return app


def main() -> None:
    """Console entry point (`aether` / `uv run aether`). A serve loop, not a
    single run: a chat-made llm.* change arms restart.request(), which flips
    this server's should_exit once the current turn has had time to land its
    words; the loop then serves a fresh app (fresh lifespan, fresh boot
    merge) without the process ever leaving the container — nobody restarts
    Aether by hand. A real SIGINT/SIGTERM never arms a restart, so the loop
    exits."""
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    while True:
        server = uvicorn.Server(
            uvicorn.Config(create_app(), host="0.0.0.0", port=port, log_level="info")
        )
        restart.register(server)
        server.run()
        if not restart.take_pending():
            break
