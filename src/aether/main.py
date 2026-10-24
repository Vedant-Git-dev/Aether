"""FastAPI app factory + startup wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import restart
from .agent import AgentLoop, CaptureRequestBox, SurfaceFanout, register_native_tools
from .agent.settings import AgentSettings
from .agent.traces import Traces
from .api import router as api_router
from .authz.approvals import Approvals
from .authz.audit import AuditLog
from .authz.policy import Policy, PolicyError
from .chat import ChatHistory, ChatHub
from .chat.ws import router as chat_router
from .composio_bridge import ComposioBridge
from .config import (
    MODEL_BASE,
    MODEL_DEFAULT,
    MODEL_OFF,
    AppConfig,
    Settings,
    load_config,
)
from .config_store import ConfigManager, ConfigOverrides
from .connectors import ProcessedUpdates, build_messaging_connectors
from .connectors.attachments import AttachmentProcessor
from .connectors.mail_allowlist import MailAllowlist
from .connectors.mcp_host import MCPHost
from .connectors.registry import ToolRegistry
from .connectors.screenvision import ScreenVision
from .counters import DailyCounters
from .llm.base import ProviderError
from .llm.embeddings import GeminiEmbedder
from .llm.model_list import ModelCatalog, is_sentinel
from .llm.registry import ProviderRegistry
from .logging_setup import configure_logging
from .media.editing import MediaEditor
from .media.frames import FrameWatcher
from .media.generate import ImageGenerator
from .media.store import MediaStore
from .media.voice import GroqTranscriber
from .memory.context import ContextBuilder
from .memory.crypto import Cipher, CryptoError, generate_key_b64
from .memory.db import create_pool, run_migrations
from .memory.entities import Entities, LLMSamePersonJudge
from .memory.events import EventStore
from .memory.salience import LLMJudge, Salience
from .memory.vectors import VectorMemory
from .proactive import Budget, ProactiveStore, ProactiveWorker
from .routines import Routines
from .scheduler import Scheduler, SchedulerWorker
from .secret_env import EnvResolver
from .secret_store import SecretStore
from .skills import build_skills
from .web import WebTools
from .workbench import Workbench, ordered
from .workbench import run as run_workbench
from .workbench.base import JobLimits, JobResult, JobSpec
from .workbench.github_actions import GitHubActionsWorkbench
from .workbench.minutes import MinuteMeter
from .workbench.modal import ModalWorkbench
from .workbench.transfers import TransferStore
from .workbench.vercel import VercelWorkbench
from .workspace import Workspace, WorkspaceWriteError

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

        # --- database ------------------------------------------------------
        pool = await create_pool(settings.database_url)
        applied = await run_migrations(pool)
        log.info("database ready (migrations applied this boot: %s)", applied or "none")

        # --- secrets by name + the app hub ---------------------------------
        # keys pasted in the /apps walk (consumed before ingest) live
        # encrypted like everything else; the resolver is how every act
        # reads a key — by name, fresh — so a key added after boot needs no
        # restart. The Composio bridge rides it: a COMPOSIO_API_KEY pasted
        # in chat works without a boot.
        secret_store = SecretStore(pool, cipher)
        env_resolver = EnvResolver(secrets=secret_store)
        app.state.secrets = secret_store
        app.state.resolver = env_resolver
        # the Settings page's model dropdowns ask this for the models each
        # configured key can reach — fetched live per provider, cached by
        # key, so a pasted key lists its models without a restart
        app.state.model_catalog = ModelCatalog(env_resolver)
        composio = ComposioBridge(env_resolver, secret_store)
        app.state.composio = composio
        try:
            if await composio.ensure():
                log.info("composio session open — the app hub is live")
        except Exception:
            # the hub being down must never keep Aether from booting —
            # /apps answers honestly and the next connect retries
            log.exception("composio session couldn't open — apps connect on demand")

        # --- chat-made config: rows merge onto the live config, in place ----
        # Before anything reads a section — an authz or allowlist override
        # must already be in effect when the first Policy and store are built.
        config_manager = ConfigManager(ConfigOverrides(pool, cipher), settings, config)
        await config_manager.bootstrap()
        app.state.config_manager = config_manager

        try:
            policy = Policy(config.authz.rules)
        except PolicyError as exc:
            raise StartupError(f"bad authz rule: {exc}") from exc

        app.state.pool = pool
        app.state.cipher = cipher
        app.state.settings = settings
        app.state.config = config

        # --- daily counters + the web tools ---------------------------------
        # counts only (search cap today, the proactive budgets later) and
        # the hardened fetch/search pair — both live on the resolver, so
        # a key pasted after boot connects without a restart.
        counters = DailyCounters(pool)
        app.state.counters = counters
        web_tools = WebTools(resolver=env_resolver, counters=counters, config=config)
        app.state.web = web_tools

        # --- the workbench: where a job runs when it must not run here ----
        # every provider is inert without its keys, so the ladder is safe
        # to build unconditionally — each provider answers for itself.
        # The batch queue additionally needs its own repository named and
        # this instance's public address (the one-time URLs it hands a
        # runner have to point somewhere); without them it stays
        # unconstructed and the ladder simply never sees it.
        transfers = TransferStore(pool, cipher)
        app.state.transfers = transfers
        sandboxes: list[Workbench] = [
            ModalWorkbench(resolver=env_resolver),
            VercelWorkbench(resolver=env_resolver),
        ]
        actions = None
        if (
            config.workbench.actions_repo
            and config.workbench.actions_workflow
            and settings.public_url
        ):
            actions = GitHubActionsWorkbench(
                resolver=env_resolver,
                transfers=transfers,
                public_url=settings.public_url,
                repo=config.workbench.actions_repo,
                workflow=config.workbench.actions_workflow,
                ref=config.workbench.actions_ref,
                monthly_minutes_cap=config.workbench.monthly_minutes_cap,
                minutes=MinuteMeter(pool),
            )
            sandboxes.append(actions)
        ladder = ordered(sandboxes, config.workbench.provider)

        async def _run_job(spec: JobSpec) -> JobResult:
            """One job through the routing ladder, with the configured job
            timeout as a ceiling every spec is clamped to — a skill may
            ask for less, never more."""
            limits = JobLimits(
                timeout_seconds=min(
                    spec.limits.timeout_seconds, config.workbench.job_timeout_minutes * 60
                ),
                max_output_bytes=spec.limits.max_output_bytes,
            )
            return await run_workbench(replace(spec, limits=limits), ladder)

        # the perception ladder's frames rung — the same job ladder every
        # other sandbox job travels, under the media job budget
        frame_watcher = FrameWatcher(_run_job, counters, config.media)

        # --- vector memory: free embeddings make recall semantic ----------
        # best effort, same as the providers — without a key the layer is a
        # quiet no-op and memory search reports the failure plainly. The
        # seam speaks Gemini only: a value pointing at another provider
        # says so and leaves the layer off rather than guessing.
        embedder = None
        knob = config.llm.embedding_model
        if knob and knob not in (MODEL_BASE, MODEL_DEFAULT, MODEL_OFF):
            provider, sep, embed_model = knob.partition("/")
            if not sep:  # a bare id — the embedding seam has only ever spoken Gemini
                provider, embed_model = "gemini", knob
            if provider != "gemini":
                log.warning(
                    "embedding model set to a %s model — memory search needs "
                    "Gemini, so it stays off",
                    provider,
                )
            elif settings.gemini_api_key:
                try:
                    embedder = GeminiEmbedder.build(settings.gemini_api_key, embed_model)
                except ProviderError as exc:
                    log.warning(
                        "embedding model unavailable (%s) — memory search is "
                        "unavailable until the key is set",
                        exc,
                    )
        vectors = VectorMemory(pool, cipher, embedder)
        app.state.vectors = vectors

        # --- memory + authz ------------------------------------------------
        audit = AuditLog(pool)
        events = EventStore(pool, cipher, audit, config.contacts, indexer=vectors)
        approvals = Approvals(pool, cipher, audit, config.authz.approval_ttl_hours)
        agent_settings = AgentSettings(pool, cipher)
        app.state.audit = audit
        app.state.events = events
        app.state.approvals = approvals
        app.state.agent_settings = agent_settings

        # --- connectors: MCP host + the flat tool namespace -----------------
        host = MCPHost(config.mcp_servers, resolver=env_resolver)
        host.start()
        app.state.mcp_host = host
        tools = ToolRegistry()
        tools.attach_mcp(host)
        # the gmail allowlist rides every bulk read the namespace executes
        mail_filter = MailAllowlist(config.contacts, audit)
        tools.attach_mail_filter(mail_filter)
        log.info("mcp tools in the namespace: %d", tools.sync_mcp_tools())
        app.state.tools = tools

        # --- encrypted media storage ----------------------------------------
        # shared files, screen captures, and later generated images become
        # AES-GCM blobs bound to their row, kept under a quota with
        # least-recently-use eviction; references from pending approvals
        # and undelivered results survive the broom. Wired here because
        # the perception layers below store into it.
        media_store = MediaStore(pool, cipher, config.media)
        app.state.media_store = media_store

        # the once-only word on platform updates — long polling's offset
        # lives only in memory, so the connectors need the durable
        # ledger to know a redelivery from a fresh message
        processed_updates = ProcessedUpdates(pool)

        # whether any sandbox on the ladder could take an edit job —
        # asked before any bytes travel, because editing has no
        # degraded fallback to fall back to
        async def _ffmpeg_capable() -> bool:
            for sandbox in ladder:
                if await sandbox.available() and sandbox.has_ffmpeg:
                    return True
            return False

        # the edit job's runner — same ladder, same media job budget
        editor = MediaEditor(_run_job, counters, config.media, media_store, _ffmpeg_capable)
        app.state.media_editor = editor

        # the image-maker's runner — keyless Pollinations by default, the
        # AI Studio key as the quality rung; every made image is a stored
        # file, capped per day
        image_generator = ImageGenerator(env_resolver, counters, config, media_store)
        app.state.image_generator = image_generator

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
        app.state.screen_vision = (
            ScreenVision(providers, events, media=media_store) if providers else None
        )
        # shared chat files ride the same rails — the loop's drain calls it
        # (the transcriber resolves GROQ_API_KEY fresh per note, so a paste
        # applies without a restart and an unset key just leaves notes kept)
        app.state.attachments = (
            AttachmentProcessor(
                providers,
                events,
                indexer=vectors,
                media=media_store,
                voice=GroqTranscriber(env_resolver),
                frames=frame_watcher,
            )
            if providers
            else None
        )

        judge_provider = providers.for_role("salience") if providers else None
        entities = Entities(pool, cipher, audit, LLMSamePersonJudge(judge_provider))
        salience = Salience(events, LLMJudge(judge_provider), config.salience)
        context = ContextBuilder(events, entities, approvals, config.agent)
        scheduler = Scheduler(pool, cipher, audit)
        app.state.entities = entities
        app.state.scheduler = scheduler
        # the proactive worker's task rows — the same audit chain as
        # everything else; the worker and its skills land later in boot
        proactive_store = ProactiveStore(pool, cipher, audit)
        app.state.proactive_store = proactive_store
        routines = Routines(pool, cipher, audit)
        app.state.routines = routines
        traces = Traces(pool, cipher)
        app.state.traces = traces

        # --- workspace: the human-editable Markdown identity/memory layer ---
        # an enhancement layer, not core plumbing: a workspace that can't be
        # set up (bad permissions, a full disk) degrades to "no workspace"
        # rather than taking the whole boot down with it. Encrypted at rest
        # by default — except under the throwaway dev key, which changes
        # every boot and would eat the files on the next restart.
        workspace: Workspace | None = None
        if config.workspace.enabled:
            encrypt = config.workspace.encrypted and bool(settings.encryption_key)
            if config.workspace.encrypted and not encrypt:
                log.warning(
                    "workspace staying plain text — the ephemeral dev key would "
                    "make encrypted files unreadable on the next boot"
                )
            candidate = Workspace(config.workspace.path, cipher=cipher if encrypt else None)
            try:
                first_run = candidate.ensure_scaffold()
            except WorkspaceWriteError as exc:
                log.warning("workspace unavailable (%s) — continuing without it", exc)
            else:
                workspace = candidate
                if encrypt:
                    migrated = candidate.migrate_plaintext()
                    if migrated:
                        await audit.append(
                            actor="system",
                            tool_name="workspace",
                            decision="allow",
                            outcome=f"encrypted {migrated} plaintext workspace file(s) at rest",
                        )
                log.info("workspace ready at %s (first run: %s)", workspace.root, first_run)
        app.state.workspace = workspace

        # --- chat + the agent ------------------------------------------------
        chat_history = ChatHistory(pool, cipher)
        chat_hub = ChatHub(chat_history)
        capture_box = CaptureRequestBox()
        app.state.chat_history = chat_history
        app.state.chat_hub = chat_hub
        app.state.capture_box = capture_box

        surfaces = SurfaceFanout(hub=chat_hub, history=chat_history)
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
            traces=traces,
            agent_settings=agent_settings,
            config_manager=config_manager,
            workspace=workspace,
            resolver=env_resolver,
            secret_store=secret_store,
            settings=settings,
            composio=composio,
            transcript=chat_history,
            attachments=app.state.attachments,
            vectors=vectors,
            mail_filter=mail_filter,
            media=media_store,
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
            traces=traces,
            audit=audit,
            config_manager=config_manager,
            workspace=workspace,
            vectors=vectors,
            web=web_tools,
            media=media_store,
            providers=providers,
            editor=editor,
            generator=image_generator,
        )
        log.info("native tools registered: %d", native)

        # the background worker — off by default and self-gating on
        # proactive.enabled every cycle, so it always spins and /resume
        # never needs a restart. Its own pause (the anomaly monitor, a
        # /pause) persists through the config store, and its notifications
        # ride the loop's interruption budget. The skills come from the
        # registry; each run goes through the workbench ladder and the
        # agent's own tool gate, so nothing bypasses policy or audit.
        # Built before the connectors: a task's Approve button reaches it
        # through on_task_decision, and its results reach the user through
        # the loop's own present fanout.
        async def _pause_proactive() -> None:
            await config_manager.set(
                op="set", path="proactive.enabled", value=False, source="worker"
            )

        async def _notify_proactive(text: str) -> None:
            await agent.note_to_user(text)

        proactive_worker = ProactiveWorker(
            store=proactive_store,
            budget=Budget(counters, config.proactive),
            config=config,
            skills=build_skills(),
            events=events,
            workspace=workspace,
            workbench=_run_job,
            executor=agent.execute_tool,
            notify=_notify_proactive,
            pause=_pause_proactive,
            approvals=approvals,
            carry_out=agent.decide,
            present=agent.present_result,
            media=media_store,
        )
        # the panel's verdict buttons reach the worker's own decide path —
        # the same one the chat surfaces' buttons take
        app.state.proactive_worker = proactive_worker

        connectors = build_messaging_connectors(
            settings,
            config,
            approvals=approvals,
            on_decision=agent.execute_decision,
            on_inbound=agent.handle_inbound,
            on_task_decision=proactive_worker.decide_task,
            processed=processed_updates,
        )
        surfaces.add_connectors(connectors)
        for connector in connectors:
            await connector.start()

        # the manager can now live-apply a chat-made change: swap the policy,
        # diff the connectors, diff the mcp host, re-parse quiet hours, or
        # arm a self-restart for an llm.* change
        config_manager.wire(
            loop=agent,
            surfaces=surfaces,
            tools=tools,
            host=host,
            approvals=approvals,
            resolver=env_resolver,
        )

        worker = SchedulerWorker(scheduler, agent.execute_scheduled)

        agent.start()
        worker.start()
        proactive_worker.start()
        log.info(
            "aether running — surfaces: web%s, agent loop + scheduler live",
            "".join(f", {c.name}" for c in connectors),
        )

        yield

        await proactive_worker.stop()
        await worker.stop()
        if actions is not None:
            await actions.aclose()
        await agent.stop()
        for connector in connectors:
            await connector.stop()
        await host.stop()
        await pool.close()
        log.info("aether stopped")

    app = FastAPI(title="Aether", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)
    app.include_router(chat_router)
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

    @app.get("/v1/models")
    async def list_models() -> dict:
        """The OpenAI-compatible models answer. Local capability probes ask
        this right after /health to check whether an OpenAI-style server
        lives on the port; before this route the honest answer was a 404,
        the only error line in an otherwise clean log. Model ids are not
        secrets — the same reasoning that leaves /health unguarded."""
        llm = config.llm
        # the sentinel words name meanings, never models — "default" is the
        # main model, which is already listed
        ids = {llm.model} | {
            m
            for m in (llm.image_model, llm.audio_model, llm.video_model, llm.salience_model)
            if m and not is_sentinel(m)
        }
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": llm.provider}
                for m in sorted(ids)
            ],
        }

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
