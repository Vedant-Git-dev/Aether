"""Scheduled actions — proposal §6: written to Postgres at creation time,
never held only in memory, so "send this at 6pm" survives a crash and a
restart runs everything the process missed while it was down.

A worker claims due rows atomically (status flip in the claim itself), runs
them through an injected runner, and records the outcome. The runner is the
agent's authz-gated executor — scheduling something is not a way around
review; the call is classified again at fire time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from ..memory.crypto import Cipher

log = logging.getLogger("aether.scheduler")

# status values
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

ActionRunner = Callable[["ScheduledAction"], Awaitable[str]]


def _aad(action_id: int) -> str:
    return f"scheduled_actions:payload_enc:{action_id}"


@dataclass
class ScheduledAction:
    id: int
    label: str
    run_at: datetime
    status: str
    payload: dict[str, Any]  # decrypted
    created_at: datetime


class Scheduler:
    """Database-backed store for scheduled actions."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def create(
        self, *, label: str, run_at: datetime, payload: dict[str, Any], actor: str = "agent"
    ) -> ScheduledAction:
        """Persist one action. The row exists the moment this returns —
        a crash between here and fire time loses nothing."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO scheduled_actions (label, run_at, status, payload_enc)"
                " VALUES ($1, $2, 'pending', $3) RETURNING id, created_at",
                label,
                run_at,
                b"",  # placeholder until the id exists; replaced below, same transaction
            )
            action_id = row["id"]
            blob = self._cipher.encrypt_json(payload, aad=_aad(action_id))
            meta_blob = self._cipher.encrypt_json(
                {"label": label, "run_at": run_at.isoformat()},
                aad=f"scheduled_actions:meta_enc:{action_id}",
            )
            await conn.execute(
                "UPDATE scheduled_actions SET payload_enc = $1, meta_enc = $2,"
                " key_version = $3 WHERE id = $4",
                blob,
                meta_blob,
                self._cipher.key_id,
                action_id,
            )
        await self._audit.append(
            actor=actor,
            tool_name="schedule_action",
            decision="info",
            params={"label": label, "run_at": run_at.isoformat()},
            outcome="action scheduled",
        )
        return ScheduledAction(
            id=action_id,
            label=label,
            run_at=run_at,
            status=PENDING,
            payload=dict(payload),
            created_at=row["created_at"],
        )

    async def get(self, action_id: int) -> ScheduledAction | None:
        row = await self._pool.fetchrow(
            "SELECT id, label, meta_enc, run_at, status, payload_enc, created_at"
            " FROM scheduled_actions WHERE id = $1",
            action_id,
        )
        return self._to_action(row) if row else None

    async def claim_due(self, limit: int = 20) -> list[ScheduledAction]:
        """Pick the due pending rows in memory, then claim them atomically.
        The run time lives in the sealed meta, so SQL cannot order by it;
        the pending set is small enough to decrypt, and the claim still
        flips status in the same statement that selects the rows."""
        now = datetime.now(UTC)
        rows = await self._pool.fetch(
            "SELECT id, label, meta_enc, run_at, status, payload_enc, created_at"
            " FROM scheduled_actions WHERE status = 'pending'"
        )
        due: list[tuple[datetime, int]] = []
        for row in rows:
            action = self._to_action(row)
            if action.run_at <= now:
                due.append((action.run_at, action.id))
        due.sort(key=lambda pair: pair[0])
        ids = [action_id for _, action_id in due[:limit]]
        if not ids:
            return []
        claimed = await self._pool.fetch(
            "UPDATE scheduled_actions SET status = 'running'"
            " WHERE id = ANY($1::bigint[]) AND status = 'pending'"
            " RETURNING id, label, meta_enc, run_at, status, payload_enc, created_at",
            ids,
        )
        return [self._to_action(r) for r in claimed]

    async def overdue_pending(self) -> list[ScheduledAction]:
        """Still-pending rows that are already due — what `re_arm` reports
        on boot (the worker's next claim runs them; listing without claiming
        keeps re_arm read-only and safe to call anytime)."""
        now = datetime.now(UTC)
        rows = await self._pool.fetch(
            "SELECT id, label, meta_enc, run_at, status, payload_enc, created_at"
            " FROM scheduled_actions WHERE status = 'pending'"
        )
        actions = [self._to_action(row) for row in rows]
        actions = [action for action in actions if action.run_at <= now]
        actions.sort(key=lambda action: action.run_at)
        return actions

    async def mark(self, action_id: int, status: str, result_digest: str = "") -> None:
        await self._pool.execute(
            "UPDATE scheduled_actions SET status = $1, result_digest = $2 WHERE id = $3",
            status,
            result_digest,
            action_id,
        )

    async def list(self, limit: int = 50) -> list[ScheduledAction]:
        """Newest first, any status — the web panel's view."""
        rows = await self._pool.fetch(
            "SELECT id, label, meta_enc, run_at, status, payload_enc, created_at"
            " FROM scheduled_actions ORDER BY id DESC LIMIT $1",
            limit,
        )
        return [self._to_action(r) for r in rows]

    def _to_action(self, row: asyncpg.Record) -> ScheduledAction:
        payload = self._cipher.decrypt_json(row["payload_enc"], aad=_aad(row["id"]))
        label, run_at = str(row["label"]), row["run_at"]
        blob = row.get("meta_enc")
        if blob:
            try:
                meta = self._cipher.decrypt_json(
                    blob, aad=f"scheduled_actions:meta_enc:{row['id']}"
                )
                label = str(meta.get("label", label))
                run_at = datetime.fromisoformat(str(meta.get("run_at", run_at)))
            except Exception:
                log.warning(
                    "scheduled action %s meta undecryptable — using the plaintext copy",
                    row["id"],
                )
        return ScheduledAction(
            id=row["id"],
            label=label,
            run_at=run_at,
            status=row["status"],
            payload=payload,
            created_at=row["created_at"],
        )


async def process_due(scheduler: Scheduler, run: ActionRunner) -> int:
    """Claim and run everything due. Failures are recorded per action and
    never raised — one broken action can't stop the others."""
    ran = 0
    for action in await scheduler.claim_due():
        try:
            result = await run(action)
            digest = hashlib.sha256(result.encode("utf-8")).hexdigest()[:16]
            await scheduler.mark(action.id, DONE, digest)
            log.info("scheduled action %d done", action.id)
        except Exception as exc:
            log.exception("scheduled action %d failed", action.id)
            # the column is a digest, and an executor's error can quote the
            # action's own content — hash it rather than write it down
            digest = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:16]
            await scheduler.mark(action.id, FAILED, digest)
        ran += 1
    return ran


class SchedulerWorker:
    """Background loop: re-arm on boot, then claim due rows every interval."""

    def __init__(self, scheduler: Scheduler, run: ActionRunner, interval: float = 20.0) -> None:
        self._scheduler = scheduler
        self._run = run
        self._interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="scheduler-worker")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    async def run_forever(self) -> None:
        # re-arm: rows that came due while the process was down run first —
        # restart survival is the whole point of persisting actions
        overdue = await self._scheduler.overdue_pending()
        if overdue:
            log.info(
                "re-arming %d overdue scheduled action(s): %s",
                len(overdue),
                ", ".join(f"{a.label} (due {a.run_at.isoformat()})" for a in overdue[:5]),
            )
        while not self._stop.is_set():
            try:
                await process_due(self._scheduler, self._run)
            except Exception:
                log.exception("scheduler pass failed — retrying next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
        log.info("scheduler worker stopped (utc now: %s)", datetime.now(UTC).isoformat())
