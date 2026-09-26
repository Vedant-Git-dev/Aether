"""FastAPI app factory + startup wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .agent import AgentLoop, CaptureRequestBox, SurfaceFanout, register_native_tools
from .api import router as api_router
from .authz.approvals import Approvals
from .authz.audit import AuditLog
from .authz.policy import Policy, PolicyError
from .chat import ChatHistory, ChatHub
from .chat.ws import router as chat_router
from .config import AppConfig, Settings, load_config
from .connectors import build_messaging_connectors
from .connectors.mcp_host import MCPHost
from .connectors.registry import ToolRegistry
from .connectors.screenvision import ScreenVision
from .llm.base import ProviderError
from .llm.registry import ProviderRegistry
from .logging_setup import configure_logging
from .memory.context import ContextBuilder
from .memory.crypto import Cipher, CryptoError, generate_key_b64
from .memory.db import create_pool, run_migrations
from .memory.entities import Entities, LLMSamePersonJudge
from .memory.events import EventStore
from .memory.salience import LLMJudge, Salience
from .routines import Routines
from .scheduler import Scheduler, SchedulerWorker

log = logging.getLogger("aether.main")

WEB_DIR = Path(__file__).parent / "web"


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
        try:
            policy = Policy(config.authz.rules)
        except PolicyError as exc:
            raise StartupError(f"bad authz rule: {exc}") from exc

        # --- database ------------------------------------------------------
        pool = await create_pool(settings.database_url)
        applied = await run_migrations(pool)
        log.info("database ready (migrations applied this boot: %s)", applied or "none")

        app.state.pool = pool
        app.state.cipher = cipher
        app.state.settings = settings
        app.state.config = config

        # --- memory + authz ------------------------------------------------
        audit = AuditLog(pool)
        events = EventStore(pool, cipher, audit, config.contacts)
        approvals = Approvals(pool, cipher, audit, config.authz.approval_ttl_hours)
        app.state.audit = audit
        app.state.events = events
        app.state.approvals = approvals

        # --- connectors: MCP host + the flat tool namespace -----------------
        host = MCPHost(config.mcp_servers)
        host.start()
        app.state.mcp_host = host
        tools = ToolRegistry()
        tools.attach_mcp(host)
        log.info("mcp tools in the namespace: %d", tools.sync_mcp_tools())
        app.state.tools = tools

        # --- llm providers (best effort: the agent boots without keys) ----
        try:
            providers = ProviderRegistry.from_config(settings, config.llm)
        except ProviderError as exc:
            providers = None
            log.warning(
                "LLM provider unavailable (%s) — chat replies, salience judging, "
                "and screen vision stay off until the keys are configured",
                exc,
            )
        app.state.providers = providers
        app.state.screen_vision = ScreenVision(providers, events) if providers else None

        judge_provider = providers.for_role("salience") if providers else None
        entities = Entities(pool, cipher, audit, LLMSamePersonJudge(judge_provider))
        salience = Salience(events, LLMJudge(judge_provider), config.salience)
        context = ContextBuilder(events, entities, approvals, config.agent)
        scheduler = Scheduler(pool, cipher, audit)
        app.state.scheduler = scheduler
        routines = Routines(pool, cipher, audit)
        app.state.routines = routines

        # --- chat + the agent ------------------------------------------------
        chat_history = ChatHistory(pool, cipher)
        chat_hub = ChatHub(chat_history)
        capture_box = CaptureRequestBox()
        app.state.chat_history = chat_history
        app.state.chat_hub = chat_hub
        app.state.capture_box = capture_box

        surfaces = SurfaceFanout(hub=chat_hub)
        agent = AgentLoop(
            providers=providers,
            tools=tools,
            policy=policy,
            approvals=approvals,
            audit=audit,
            events=events,
            salience=salience,
            context=context,
            scheduler=scheduler,
            surfaces=surfaces,
            capture_box=capture_box,
            config=config,
            host=host,
            routines=routines,
        )
        app.state.agent = agent
        native = register_native_tools(
            registry=tools,
            events=events,
            entities=entities,
            approvals=approvals,
            scheduler=scheduler,
            surfaces=surfaces,
            capture_box=capture_box,
            routines=routines,
        )
        log.info("native tools registered: %d", native)

        connectors = build_messaging_connectors(
            settings,
            config,
            approvals=approvals,
            on_decision=agent.execute_decision,
            on_inbound=agent.submit_message,
        )
        surfaces.add_connectors(connectors)
        for connector in connectors:
            await connector.start()

        worker = SchedulerWorker(scheduler, agent.execute_scheduled)
        agent.start()
        worker.start()
        log.info(
            "aether running — surfaces: web%s, agent loop + scheduler live",
            "".join(f", {c.name}" for c in connectors),
        )

        yield

        await worker.stop()
        await agent.stop()
        for connector in connectors:
            await connector.stop()
        await host.stop()
        await pool.close()
        log.info("aether stopped")

    app = FastAPI(title="Aether", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    app.include_router(chat_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        """Plain 200 — this is the endpoint the external uptime pinger hits."""
        return {"ok": True}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


def main() -> None:
    """Console entry point (`aether` / `uv run aether`)."""
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
