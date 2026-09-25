"""FastAPI app factory + startup wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import AppConfig, Settings, load_config
from .logging_setup import configure_logging
from .memory.crypto import Cipher, CryptoError, generate_key_b64
from .memory.db import create_pool, run_migrations

log = logging.getLogger("aether.main")


class StartupError(RuntimeError):
    """Fatal misconfiguration — boot refuses to continue."""


def _build_cipher(settings: Settings) -> Cipher:
    if settings.encryption_key:
        try:
            return Cipher.from_b64(settings.encryption_key)
        except CryptoError as exc:
            raise StartupError(f"AETHER_ENCRYPTION_KEY is invalid: {exc}") from exc
    if settings.dev_ephemeral_key:
        log.warning(
            "AETHER_ENCRYPTION_KEY not set — using an EPHEMERAL key. "
            "Encrypted data will be unreadable after a restart. "
            "Set AETHER_DEV_EPHEMERAL_KEY=0 and configure a real key for anything real."
        )
        return Cipher.from_b64(generate_key_b64())
    raise StartupError(
        "AETHER_ENCRYPTION_KEY is not set. Generate one with:\n"
        '  python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"\n'
        "(or set AETHER_DEV_EPHEMERAL_KEY=1 for a throwaway dev instance)"
    )


def create_app(
    settings: Settings | None = None,
    config_path: str | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    config: AppConfig = load_config(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- fail-fast validation -----------------------------------------
        if not settings.database_url:
            raise StartupError(
                "DATABASE_URL is not set. Aether persists everything to Postgres — "
                "create a free Neon database and put its connection string in .env."
            )
        cipher = _build_cipher(settings)

        # --- database ------------------------------------------------------
        pool = await create_pool(settings.database_url)
        applied = await run_migrations(pool)
        log.info("database ready (migrations applied this boot: %s)", applied or "none")

        app.state.pool = pool
        app.state.cipher = cipher
        app.state.settings = settings
        app.state.config = config

        # Components wire in here as they land: MCP host, messaging
        # connectors, scheduler worker, agent loop.

        yield

        await pool.close()
        log.info("aether stopped")

    app = FastAPI(title="Aether", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Plain 200 — this is the endpoint the external uptime pinger hits."""
        return {"ok": True}

    return app


def main() -> None:
    """Console entry point (`aether` / `uv run aether`)."""
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
