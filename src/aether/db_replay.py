"""Load replay: one synthetic day of Aether traffic against a scratch database.

The replay builds the real stores — event index, vector memory, lexical index,
approvals, deliveries, scheduler, provider cursors, alerts, reminders — seeds a
synthetic history, then walks a simulated day minute by minute. It mirrors the
database calls the agent loop, the scheduler worker, the provider pollster and
the panel make, without calling a model or a provider. The point is to measure
database work, not to answer anyone.

The meters are the production `DBAccounting` layer, driven by a virtual clock,
so the hourly pattern and the idle gaps are the simulated day's, not the
wall-clock time of the run. Response bytes are estimated from returned records;
active compute is the union of "a query happened, then the provider's
autosuspend window", which is a model of suspension behavior, not a reading of
its meter.

Safety: the URL's database name must look like a scratch database. The replay
truncates every application table, so it refuses `aether` (the live database)
and any name without scratch/test/ci/replay in it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import yaml

from .agent.traces import Traces
from .alerts.engine import AlertEngine
from .alerts.notifications import AlertNotifications
from .alerts.store import AlertStore
from .authz.approvals import Approvals
from .authz.audit import AuditLog
from .chat.ws import ChatHistory
from .config import AppConfig
from .connectors.polling import next_poll_delay
from .db_accounting import DBAccounting, MeteredPool
from .memory.commitments import Commitments
from .memory.crypto import Cipher, generate_key_b64
from .memory.db import run_migrations
from .memory.deliveries import Deliveries
from .memory.entities import Entities
from .memory.events import EventStore
from .memory.facts import Facts
from .memory.inbound import DbInboundStates
from .memory.indexes import MemoryIndex
from .memory.lexical import LexicalIndex
from .memory.pipeline import QueryPipeline, retrieval_config_from
from .memory.storage import database_size_bytes, enforce_schema, reset_application_tables
from .memory.summaries import Summaries
from .memory.vectors import VectorMemory
from .memory.views import Views
from .proactive.path import CommitmentReminders, ProactivePath
from .providers.crypto import TokenKeyring
from .providers.store import ProviderStore
from .providers.types import ProviderConnection
from .scheduler.jobs import Scheduler

log = logging.getLogger("aether.db_replay")

# application tables the replay may wipe — the same set the test suite owns
_TABLES = (
    "audit_log, pending_approvals, events, identities, identity_handles, "
    "entity_notes, chat_messages, scheduled_actions, routines_legacy, decision_traces, "
    "config_overrides, oauth_tokens, app_secrets, memory_chunks, daily_counters, "
    "workbench_transfers, workbench_minutes, proactive_tasks, media_blobs, "
    "processed_updates, provider_connections, provider_oauth_states, "
    "provider_sync_state, facts, commitments, commitment_reminders, alerts, "
    "alert_notifications, summaries, focus, forgotten_events, "
    "conclusions, deliveries, workspace_files, inbound_messages"
)

_SCRATCH_TAGS = ("scratch", "test", "ci", "replay")


class ReplaySafetyError(RuntimeError):
    """The URL does not name a database this harness may wipe."""


def scratch_database_name(url: str) -> str:
    """The database name, refusing a URL that could be the live store."""
    name = urlsplit(url).path.lstrip("/").strip()
    if not name:
        raise ReplaySafetyError("the replay needs an explicit database name")
    lowered = name.lower()
    if lowered == "aether":
        raise ReplaySafetyError("refusing the live `aether` database; use a scratch database")
    if not any(tag in lowered for tag in _SCRATCH_TAGS):
        raise ReplaySafetyError(
            f"refusing database {name!r}: its name must contain one of {', '.join(_SCRATCH_TAGS)}"
        )
    return name


@dataclass(frozen=True)
class ReplayOptions:
    """One simulated day's shape. Defaults are config-backed in `from_config`."""

    seed_events: int = 1500
    seed_docs: int = 120
    seed_doc_pages: int = 6
    seed_commitments: int = 12
    seed_facts: int = 30
    seed_traces: int = 200
    seed_audit: int = 100
    seed_deliveries: int = 40
    messages: int = 20
    mails: int = 12
    panel_mode: str = "poll"  # poll (baseline) | visit | off
    panel_hours: float = 2.0
    tick_seconds: float = 30.0
    scheduler_seconds: float = 20.0
    backfill_seconds: float = 60.0
    provider_poll_minutes: float = 5.0
    mcp_poll_minutes: float = 5.0
    restarts_per_day: int = 1


class _Clock:
    """A settable epoch clock — the simulated day is the accounting day."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> float:
        return self.now

    def begin(self, start: float) -> None:
        self.start = start
        self.now = start


def active_seconds(timeline: list[float], start: float, end: float, grace: float) -> float:
    """Union of [query, query + autosuspend grace], clipped to the day.

    A query wakes compute and it stays awake until the provider's idle window
    passes without another query. The result is an estimate of billed active
    time under that model, not a provider reading.
    """
    if not timeline:
        return 0.0
    moments = sorted(moment for moment in timeline if start <= moment <= end)
    if not moments:
        return 0.0
    total = 0.0
    window_start = max(start, moments[0])
    window_end = min(end, moments[0] + grace)
    for moment in moments[1:]:
        if moment > window_end:
            total += max(0.0, window_end - window_start)
            window_start = moment
            window_end = min(end, moment + grace)
        else:
            window_end = min(end, max(window_end, moment + grace))
    total += max(0.0, window_end - window_start)
    return total


@dataclass
class ReplayResult:
    """What one simulated day cost, plus the per-site detail behind it."""

    options: ReplayOptions
    snapshot: dict = field(default_factory=dict)
    active_seconds: float = 0.0
    seed_bytes: int = 0
    day_start: float = 0.0
    day_end: float = 0.0

    @property
    def totals(self) -> dict:
        return self.snapshot.get("totals") or {}

    def projections(self, config: AppConfig) -> dict:
        limits = config.db
        cu_hours = self.active_seconds / 3600.0 * limits.compute_cu
        monthly_gb = float(self.totals.get("bytes", 0)) * 30 / 1_000_000_000
        return {
            "day_cu_hours": cu_hours,
            "month_cu_hours": cu_hours * 30,
            "month_transfer_gb": monthly_gb,
            "cu_limit": limits.monthly_compute_cu_hours,
            "transfer_limit": limits.monthly_transfer_gb,
        }

    def budgets(self, config: AppConfig) -> dict:
        return {
            "queries": (int(self.totals.get("calls", 0)), config.db.daily_query_budget),
            "response_mb": (
                float(self.totals.get("bytes", 0)) / 1_000_000,
                config.db.daily_response_budget_mb,
            ),
            "active_minutes": (
                self.active_seconds / 60.0,
                float(config.db.daily_active_minutes_budget),
            ),
        }

    def over_budget(self, config: AppConfig) -> list[str]:
        return [name for name, (actual, budget) in self.budgets(config).items() if actual > budget]


def _human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1000
    return f"{value:.1f} GB"


def render(result: ReplayResult, config: AppConfig) -> str:
    """The terminal report. Every byte and compute figure is an estimate."""
    totals = result.totals
    projected = result.projections(config)
    budgets = result.budgets(config)
    options = result.options
    ticks_per_minute = len(range(0, 60, max(1, int(options.tick_seconds))))
    claims_per_minute = len(range(0, 60, max(1, int(options.scheduler_seconds))))
    lines = [
        "Aether database replay — one simulated day",
        f"  seed:          ~{_human_bytes(result.seed_bytes)} in the scratch database",
        f"  actions:       {options.messages} messages, {options.mails} mails, "
        f"{ticks_per_minute * 1440} agent ticks, "
        f"{claims_per_minute * 1440} scheduler claims, "
        f"{int(86400 / max(1, options.backfill_seconds))} index polls, "
        f"panel {options.panel_mode} for {options.panel_hours:g}h",
        "",
        f"  queries:       {int(totals.get('calls', 0)):,}",
        f"  rows:          {int(totals.get('rows', 0)):,}",
        f"  response:      ~{_human_bytes(totals.get('bytes', 0))} (estimated)",
        f"  query time:    {float(totals.get('seconds', 0.0)):.1f} s",
        f"  active time:   {result.active_seconds / 3600.0:.2f} h "
        f"(autosuspend {config.db.autosuspend_seconds}s; model, not provider meter)",
        "",
        f"  projected:     {projected['month_cu_hours']:.1f} CU-hours/month "
        f"(limit {projected['cu_limit']:g})",
        f"                 {projected['month_transfer_gb']:.2f} GB/month "
        f"(limit {projected['transfer_limit']:g})",
        "",
        "  daily budgets:",
    ]
    for name, (actual, budget) in budgets.items():
        state = "over" if actual > budget else "within"
        unit = "MB" if name == "response_mb" else ""
        lines.append(f"    {name:14} {actual:9.1f}{unit} / {budget:g}{unit}  {state}")
    hourly = result.snapshot.get("hourly") or []
    if hourly:
        lines += ["", "Queries by UTC hour"]
        busiest = max(int(value) for value in hourly) or 1
        for hour, count in enumerate(hourly):
            bar = "#" * max(0, round(int(count) / busiest * 32))
            lines.append(f"  {hour:02d}:00  {int(count):6,d}  {bar}")
    lines += ["", "Top readers by estimated response bytes"]
    for site in (result.snapshot.get("sites") or [])[:12]:
        lines.append(
            f"  {site['site']}: {int(site['calls']):,} calls, "
            f"{int(site['rows']):,} rows, ~{_human_bytes(site['bytes'])}"
        )
    by_calls = sorted(
        result.snapshot.get("sites") or [], key=lambda item: item["calls"], reverse=True
    )
    lines += ["", "Top callers by query count"]
    for site in by_calls[:10]:
        lines.append(
            f"  {site['site']}: {int(site['calls']):,} calls, "
            f"{int(site['rows']):,} rows, ~{_human_bytes(site['bytes'])}"
        )
    lines += [
        "",
        "All figures come from this harness against a scratch database. They are "
        "estimates of Neon usage, not readings of it.",
    ]
    return "\n".join(lines)


def load_options(config: AppConfig, **overrides: object) -> ReplayOptions:
    """Defaults from the database section, then explicit CLI overrides."""
    base = {
        "messages": config.db.replay_messages_per_day,
        "mails": config.db.replay_mails_per_day,
        "panel_hours": config.db.replay_panel_hours,
        "tick_seconds": float(config.agent.tick_seconds),
    }
    base.update({key: value for key, value in overrides.items() if value is not None})
    return ReplayOptions(**base)  # type: ignore[arg-type]


class _ReplayEmbedder:
    """Deterministic bag-of-words vectors — no network, stable similarity."""

    model = "replay-embedding"
    dims = 64

    def __init__(self) -> None:
        self._axes: dict[str, int] = {}

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in re.findall(r"\w+", text.lower()):
            axis = self._axes.setdefault(word, len(self._axes))
            vector[axis % self.dims] += 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector

    async def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        return [self._vector(text) for text in texts]


@dataclass
class _Bundle:
    """Every store the replay drives, wired the way the application wires them."""

    pool: MeteredPool
    cipher: Cipher
    audit: AuditLog
    events: EventStore
    vectors: VectorMemory
    index: MemoryIndex
    lexical: LexicalIndex
    views: Views
    pipeline: QueryPipeline
    approvals: Approvals
    deliveries: Deliveries
    inbound: DbInboundStates
    entities: Entities
    scheduler: Scheduler
    commitments: Commitments
    facts: Facts
    alerts: AlertStore
    alert_engine: AlertEngine
    notifications: AlertNotifications
    reminders: CommitmentReminders
    path: ProactivePath
    provider_store: ProviderStore
    traces: Traces
    transcript: ChatHistory


def _build_bundle(pool: MeteredPool, config: AppConfig) -> _Bundle:
    cipher = Cipher.from_b64(generate_key_b64(), key_id="replay")
    audit = AuditLog(pool)
    keyring = TokenKeyring.from_values(active_key_b64=generate_key_b64(), active_id="replay")
    assert keyring is not None
    vectors = VectorMemory(pool, cipher, _ReplayEmbedder())
    index = MemoryIndex()
    lexical = LexicalIndex(hot_chunks=config.memory.hot_chunks, cold_batch=config.memory.cold_batch)
    entities = Entities(pool, cipher, audit, _NoSamePerson())
    commitments = Commitments(pool, cipher, audit)
    facts = Facts(pool, cipher, audit)
    events = EventStore(
        pool,
        cipher,
        audit,
        config.contacts,
        indexer=vectors,
        index=index,
        cold_batch=config.memory.cold_batch,
    )

    def _chunk_listener(chunk: dict) -> None:
        if chunk.get("action") == "remove":
            lexical.remove_event(int(chunk["event_id"]))
            return
        lexical.add_chunk(
            chunk_id=int(chunk["chunk_id"]),
            event_id=int(chunk["event_id"]),
            ordinal=int(chunk.get("ordinal") or 0),
            source=str(chunk.get("source", "")),
            kind=str(chunk.get("kind", "")),
            filename=str(chunk.get("filename", "")),
            location=str(chunk.get("location", "")),
            occurred_at=chunk.get("occurred_at") or datetime.now(UTC),
            text=str(chunk.get("text", "")),
        )

    vectors.add_chunk_listener(_chunk_listener)
    views = Views(
        index,
        commitments,
        facts=facts,
        pool=pool,
        cipher=cipher,
        timezone=config.memory.default_timezone,
        cold_batch=config.memory.cold_batch,
        core_facts_chars=config.memory.core_facts_chars,
    )
    pipeline = QueryPipeline(
        lexical=lexical,
        index=index,
        views=views,
        vectors=vectors,
        summaries=Summaries(pool, cipher),
        cfg=retrieval_config_from(config.memory),
        timezone=config.memory.default_timezone,
    )
    alerts = AlertStore(pool, cipher, audit)
    notifications = AlertNotifications(pool, cipher)
    scheduler = Scheduler(pool, cipher, audit)
    alert_engine = AlertEngine(
        config=config,
        store=alerts,
        notifications=notifications,
        scheduler=scheduler,
        timezone=config.memory.default_timezone,
        locale=config.agent.locale,
    )
    reminders = CommitmentReminders(pool)
    path = ProactivePath(
        config=config,
        commitments=commitments,
        reminders=reminders,
        timezone=config.memory.default_timezone,
    )
    return _Bundle(
        pool=pool,
        cipher=cipher,
        audit=audit,
        events=events,
        vectors=vectors,
        index=index,
        lexical=lexical,
        views=views,
        pipeline=pipeline,
        approvals=Approvals(pool, cipher, audit, config.authz.approval_ttl_hours),
        deliveries=Deliveries(pool, cipher, audit),
        inbound=DbInboundStates(pool, cipher),
        entities=entities,
        scheduler=scheduler,
        commitments=commitments,
        facts=facts,
        alerts=alerts,
        alert_engine=alert_engine,
        notifications=notifications,
        reminders=reminders,
        path=path,
        provider_store=ProviderStore(pool, keyring, cipher),
        traces=Traces(pool, cipher),
        transcript=ChatHistory(pool, cipher),
    )


class _NoSamePerson:
    """Identity judge stand-in: never merges two people during a replay."""

    async def confirm(self, a: object, b: object) -> float:
        return 0.0


async def _seed(bundle: _Bundle, options: ReplayOptions, clock: _Clock) -> None:
    """Synthetic history with the shape (not the content) of real use."""
    start = datetime.fromtimestamp(clock.now, UTC)
    owner = await bundle.entities.resolve("gmail", "owner@example.com", "The owner")
    await bundle.entities.mark_owner(owner.id)
    event_ids: list[int] = []
    for number in range(options.seed_events):
        when = start - timedelta(hours=(number * 7) % 720)
        mail = number % 3 == 0
        text = (
            f"Project alpha update {number}: the invoice for vendor {number % 40} is due "
            f"on the fifteenth. Please review the attached summary and reply with approval. "
            + ("Background notes and context for the weekly review. " * (number % 5))
        )
        payload = (
            {
                "subject": f"Project alpha update {number}",
                "sender": f"Person {number % 25} <person{number % 25}@example.com>",
                "body": text,
                "snippet": text[:180],
            }
            if mail
            else {"text": text, "chat_ref": "web"}
        )
        result = await bundle.events.ingest(
            source="gmail" if mail else "web",
            kind="mail" if mail else "chat_message",
            payload=payload,
            occurred_at=when,
        )
        if result.event_id is not None:
            event_ids.append(result.event_id)
    await bundle.vectors.wait_idle()

    for number in range(options.seed_docs):
        when = start - timedelta(days=(number % 45) + 1)
        filename = f"deck-{number}.pdf"
        result = await bundle.events.ingest(
            source="documents",
            kind="shared_doc",
            payload={"filename": filename, "text": f"Deck {number} preview"},
            occurred_at=when,
        )
        if result.event_id is None:
            continue
        event_ids.append(result.event_id)
        pages = [
            (
                f"page {page}",
                f"Slide {page} of deck {number}. " + "Quarterly numbers and plans. " * 40,
            )
            for page in range(1, options.seed_doc_pages + 1)
        ]
        await bundle.vectors.index_doc(result.event_id, "documents", filename, pages)

    provenance = event_ids[:1] or [1]
    for number in range(options.seed_facts):
        await bundle.facts.consolidate(
            subject_identity_id=owner.id if number % 2 == 0 else None,
            key=f"preference_{number % 8}",
            value=f"the owner prefers option {number} for project alpha",
            subject="the owner",
            provenance=provenance,
            actor="system",
        )
    for number in range(options.seed_commitments):
        await bundle.commitments.add(
            kind="task",
            text=f"Send the project alpha update to the vendor {number}",
            due_at=start + timedelta(hours=(number % 48) + 1),
            urgency=number % 4,
            source_event_id=provenance[0],
        )
    await bundle.provider_store.upsert(
        ProviderConnection(
            provider="gmail",
            auth_mode="oauth",
            account_label="owner@example.com",
            access_token="replay-access",
            refresh_token="replay-refresh",
            scopes="https://www.googleapis.com/auth/gmail.readonly",
            status="active",
        )
    )
    for number in range(options.seed_traces):
        await bundle.traces.create(
            kind="turn",
            label=f"seeded turn {number}",
            payload={
                "question": f"seeded question {number} about project alpha",
                "reply": "seeded reply " * 40,
                "calls": [{"name": "memory_search", "result": "x" * 200}],
            },
        )
    for number in range(options.seed_audit):
        await bundle.audit.append(
            actor="system",
            tool_name="poll",
            decision="allow",
            outcome=f"seeded audit row {number}",
        )
    for number in range(options.seed_deliveries):
        delivery = await bundle.deliveries.record_queued("web", f"seeded reply {number}")
        if delivery is not None:
            await bundle.deliveries.mark_sent(delivery.id)
    await bundle.scheduler.create(
        label="daily digest",
        run_at=start + timedelta(hours=9),
        payload={"tool": "web__search", "params": {"q": "project alpha"}},
    )


class _ReplayDay:
    """The database calls of one day, in the order the application makes them."""

    def __init__(
        self, bundle: _Bundle, options: ReplayOptions, config: AppConfig, clock: _Clock
    ) -> None:
        self.b = bundle
        self.options = options
        self.config = config
        self.clock = clock
        self.last_event_id = 0
        self._message_number = 0
        self._mail_number = 0
        self._stamp = datetime.fromtimestamp(clock.now, UTC)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now, UTC)

    async def restart(self) -> None:
        await run_migrations(self.b.pool)
        await enforce_schema(self.b.pool, self.config.memory.schema_audit)
        await self.b.index.rebuild(
            self.b.pool, self.b.cipher, hot_limit=self.config.memory.hot_events
        )
        await self.b.lexical.rebuild(self.b.pool, self.b.cipher)
        self.last_event_id = await self.b.events.max_id()

    async def agent_tick(self) -> None:
        """The quiet-tick DB footprint: nothing new, yet every check runs."""
        now = self._now()
        await self.b.approvals.expire_overdue()
        await self.b.deliveries.undelivered(5)
        await self.b.deliveries.undelivered(5)
        await self.b.entities.owner_addresses()
        await self.b.inbound.stale_unchecked(now - timedelta(seconds=600))
        await self.b.events.list_since(self.last_event_id, limit=100)
        await self.b.path.tick(now)
        await self.b.alert_engine.tick(now)

    async def backfill_poll(self) -> None:
        """The vector backfill loop when caught up: one SELECT every minute."""
        await self.b.vectors.backfill(50)

    async def scheduler_claim(self) -> None:
        """SchedulerWorker.run_forever's pass, every interval."""
        await self.b.scheduler.claim_due()

    async def provider_poll(self) -> None:
        """The DB side of ProviderRuntime.poll_due(), with no network call.

        It reads the connection rows and both cursor values before it can
        know whether a poll is due, then writes the poll state when it is —
        exactly the shape of runtime.py:620-682 and _set_poll_state.
        """
        now = self._now()
        connections = await self.b.provider_store.list()
        if not connections:
            return
        for poll_key, interval in (
            ("new_mail", self.options.provider_poll_minutes),
            ("reconcile_mail", 60.0),
        ):
            failures_raw = await self.b.provider_store.get_sync(
                "gmail", "gmail", f"poll:{poll_key}:failures"
            )
            try:
                failures = max(0, int(failures_raw or 0))
            except (TypeError, ValueError):
                failures = 0
            delay = next_poll_delay(interval, failures=failures, jitter=0.0)
            last_raw = await self.b.provider_store.get_sync(
                "gmail", "gmail", f"poll:{poll_key}:last"
            )
            last = _parse_time(last_raw)
            if last is not None and now < last + timedelta(minutes=delay):
                continue
            await self.b.provider_store.set_sync(
                "gmail", "gmail", f"poll:{poll_key}:last", now.isoformat()
            )
            await self.b.provider_store.set_sync("gmail", "gmail", f"poll:{poll_key}:failures", "0")
            await self.b.provider_store.set_sync(
                "gmail", "gmail", f"poll:{poll_key}:last_ok", now.isoformat()
            )

    async def mcp_poll(self) -> None:
        """One configured source poll per interval: audit, then ingest.

        The real path writes the audit row and ingests the poll result even
        when the result is empty; ingest dedups the repeat, so the writes are
        the cost that remains.
        """
        await self.b.audit.append(
            actor="scheduler",
            tool_name="mail__list_messages",
            decision="allow",
            rules_matched="config:poll_tools",
            params={},
            outcome="source poll",
        )
        await self.b.events.ingest(
            source="mail",
            kind="poll:list_messages",
            payload={"args": {}, "result": ""},
            occurred_at=self._now(),
        )

    async def panel_reads(self) -> None:
        """One round of every panel list the old pollers fetched."""
        await self.b.approvals.list_pending()
        await self.b.events.recent(limit=100)
        await self.b.audit.recent(limit=200)
        await self.b.traces.recent(limit=50)

    async def panel_approvals(self) -> None:
        await self.b.approvals.list_pending()

    async def message(self) -> None:
        """One inbound message's full DB footprint, minus the model call."""
        self._message_number += 1
        number = self._message_number
        text = (
            f"Message {number}: what did we agree about the project alpha invoice "
            f"with vendor {number % 40}, and what is due next?"
        )
        now = self._now()
        result = await self.b.events.ingest(
            source="web",
            kind="chat_message",
            payload={"text": text, "chat_ref": "web"},
            occurred_at=now,
        )
        await self.b.vectors.wait_idle()
        if result.event_id is not None:
            self.last_event_id = max(self.last_event_id, result.event_id)
        await self.b.inbound.record("web", "web", f"msg-{number}")
        await self.b.transcript.append("web", "in", text)
        await self.b.events.update_salience(
            result.event_id or 0, 5.0, True, category="request", actionability="reply"
        )
        await self.b.pipeline.run(text, now=now)
        reply = f"Reply {number}: the invoice is due on the fifteenth; I will remind you."
        await self.b.transcript.append("web", "out", reply)
        delivery = await self.b.deliveries.record_queued("web", reply)
        if delivery is not None:
            await self.b.deliveries.mark_sent(delivery.id)
        await self.b.traces.create(
            kind="turn",
            label=f"turn {number}",
            payload={
                "question": text,
                "reply": reply,
                "calls": [{"name": "memory_search", "result": "ok"}],
                "notes": ["replay"],
            },
        )

    async def mail(self) -> None:
        """One mail arrival: ingest, chunk/embed, score — nothing else."""
        self._mail_number += 1
        number = self._mail_number
        text = (
            f"Mail {number}: the invoice from vendor {number % 40} needs approval. "
            + "Please review the summary and reply before the deadline. " * 6
        )
        result = await self.b.events.ingest(
            source="gmail",
            kind="mail",
            payload={
                "subject": f"Invoice {number} needs approval",
                "sender": f"Person {number % 25} <person{number % 25}@example.com>",
                "body": text,
                "snippet": text[:200],
            },
            occurred_at=self._now(),
        )
        await self.b.vectors.wait_idle()
        if result.event_id is not None:
            self.last_event_id = max(self.last_event_id, result.event_id)
            await self.b.events.update_salience(result.event_id, 4.0, False)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day_minutes(count: int, start_minute: int, end_minute: int) -> list[int]:
    """Evenly spread deterministic arrival minutes inside a working window."""
    if count <= 0:
        return []
    span = max(1, end_minute - start_minute)
    return [start_minute + int(index * span / count) for index in range(count)]


async def _simulate(day: _ReplayDay, options: ReplayOptions) -> None:
    """Walk one simulated day; the virtual clock drives the meters' buckets."""
    start = day.clock.start
    messages = _day_minutes(options.messages, 7 * 60, 22 * 60)
    mails = _day_minutes(options.mails, 0, 1440)
    panel_start = 9 * 60
    panel_end = min(1439, panel_start + int(options.panel_hours * 60))
    tick = max(1, int(options.tick_seconds))
    scheduler = max(1, int(options.scheduler_seconds))

    def at(second_of_day: float) -> None:
        day.clock.now = start + second_of_day

    for minute in range(1440):
        at(minute * 60)
        if minute == 0 and options.restarts_per_day > 0:
            await day.restart()
        for offset in range(0, 60, tick):
            at(minute * 60 + offset)
            await day.agent_tick()
        for offset in range(0, 60, scheduler):
            at(minute * 60 + offset)
            await day.scheduler_claim()
        if (minute * 60) % max(1, int(options.backfill_seconds)) == 0:
            at(minute * 60 + 15)
            await day.backfill_poll()
        if minute % max(1, int(options.provider_poll_minutes)) == 0:
            at(minute * 60)
            await day.provider_poll()
        if minute % max(1, int(options.mcp_poll_minutes)) == 0:
            at(minute * 60 + 12)
            await day.mcp_poll()
        if options.panel_mode == "visit":
            if minute in (panel_start, panel_end):
                at(minute * 60 + 5)
                await day.panel_reads()
        elif options.panel_mode == "poll" and panel_start <= minute < panel_end:
            for offset in range(0, 60, 5):
                at(minute * 60 + offset)
                if offset % 10 == 0:
                    await day.panel_reads()
                else:
                    await day.panel_approvals()
        if minute in messages:
            at(minute * 60 + 3)
            await day.message()
        if minute in mails:
            at(minute * 60 + 7)
            await day.mail()


async def run_replay(
    url: str, config: AppConfig, options: ReplayOptions | None = None
) -> ReplayResult:
    """Seed a scratch database and replay one day against it."""
    scratch_database_name(url)
    options = options or load_options(config)
    start = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    clock = _Clock(start)
    clock.begin(start)
    raw = await asyncpg.create_pool(url, min_size=1, max_size=2, command_timeout=120)
    try:
        await run_migrations(raw)
        await reset_application_tables(raw, _TABLES)
        accounting = DBAccounting(clock=clock, collect_timeline=True)
        pool = MeteredPool(raw, accounting)
        bundle = _build_bundle(pool, config)
        await _seed(bundle, options, clock)
        seed_bytes = await database_size_bytes(raw)
        accounting.reset()
        day = _ReplayDay(bundle, options, config, clock)
        await _simulate(day, options)
        snapshot = accounting.snapshot()
        timeline = accounting.timeline()
        return ReplayResult(
            options=options,
            snapshot=snapshot,
            active_seconds=active_seconds(
                timeline, start, start + 86_400, float(config.db.autosuspend_seconds)
            ),
            seed_bytes=seed_bytes,
            day_start=start,
            day_end=start + 86_400,
        )
    finally:
        await raw.close()


def load_config_explicit(path: str | None) -> AppConfig:
    """Read a config file only when the caller names one.

    The harness never reads the repository's `config.yaml` by default: that
    file belongs to the operator and may sit beside secrets. Without a path,
    the code defaults apply and the report says so.
    """
    if not path:
        return AppConfig()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aether.db_replay",
        description="Replay one synthetic day against a scratch Postgres and report database use.",
    )
    parser.add_argument("--url", default=os.environ.get("AETHER_TEST_DATABASE_URL", ""))
    parser.add_argument("--config", default="", help="an explicit config.yaml for budgets")
    parser.add_argument("--seed-events", type=int)
    parser.add_argument("--seed-docs", type=int)
    parser.add_argument("--messages", type=int)
    parser.add_argument("--mails", type=int)
    parser.add_argument("--panel-mode", choices=("poll", "visit", "off"))
    parser.add_argument("--panel-hours", type=float)
    parser.add_argument("--tick-seconds", type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.url:
        print("pass --url or set AETHER_TEST_DATABASE_URL (scratch database only)", file=sys.stderr)
        return 2
    config = load_config_explicit(args.config or None)
    options = load_options(
        config,
        seed_events=args.seed_events,
        seed_docs=args.seed_docs,
        messages=args.messages,
        mails=args.mails,
        panel_mode=args.panel_mode,
        panel_hours=args.panel_hours,
        tick_seconds=args.tick_seconds,
    )
    try:
        result = asyncio.run(run_replay(args.url, config, options))
    except ReplaySafetyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "options": options.__dict__,
            "snapshot": result.snapshot,
            "active_seconds": result.active_seconds,
            "seed_bytes": result.seed_bytes,
            "projections": result.projections(config),
            "budgets": result.budgets(config),
            "over_budget": result.over_budget(config),
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(render(result, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
