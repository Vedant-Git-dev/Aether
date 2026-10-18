"""The proactive task store — rows in Postgres, written at the moment
anything is decided about a task, never held only in memory.

The lifecycle is one row per opportunity, ever (the unique index is the
dedup): ``pending`` the instant a cycle claims it, ``running`` while the
skill plans and runs, ``done`` or ``failed`` after — so a crash anywhere
leaves a row that says exactly where the work stopped, and the boot re-arm
turns ``running`` back into ``pending`` to try again. Feedback (approved,
revised, discarded — the user's one-tap verdict, with an optional note)
stays on the same row: selection bias is derived from it, never stored
separately.

Everything with content is AES-GCM encrypted and bound to its row through
the cipher's AAD — the opportunity detail, the plan, the result, the
feedback note — the same pattern every other store uses. The plain columns
are routing metadata: skill, label, status, which provider ran it, which
local day it was claimed on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from ..memory.crypto import Cipher

log = logging.getLogger("aether.proactive")

# status values — the row's one word for where the work stopped
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# the one-tap feedback values (the notification buttons land here)
APPROVED = "approved"
REVISED = "revised"
DISCARDED = "discarded"


def _aad(column: str, task_id: int) -> str:
    return f"proactive_tasks:{column}:{task_id}"


@dataclass
class Opportunity:
    """One thing a skill found worth doing. The key is the stable identity
    the store dedups on — derived deterministically from the skill and its
    source identifiers (a thread id, an event id, a file id), so the same
    thread can never become two tasks across cycles. The detail is what
    the plan step re-reads: a few plain fields, never the full content."""

    key: str
    skill: str
    label: str
    score: float = 0.0  # the skill's own 0-10 worth-doing estimate
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProactiveTask:
    id: int
    skill: str
    label: str
    status: str
    opportunity_key: str
    opportunity: dict[str, Any]  # decrypted detail; {} when the blob is empty
    plan: dict[str, Any] | None  # decrypted, once the skill planned
    result: str | None  # decrypted, once the task settled
    feedback: str | None
    feedback_note: str | None
    provider: str | None
    day: str
    created_at: datetime
    updated_at: datetime


class ProactiveStore:
    """proactive_tasks rows. Every transition is a single UPDATE, so a
    crash between steps can only ever leave a row that says the truth."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def claim(
        self,
        *,
        skill: str,
        key: str,
        label: str,
        opportunity: dict[str, Any],
        day: str,
        actor: str = "worker",
    ) -> ProactiveTask | None:
        """Take an opportunity: insert a pending row, or return None when
        the key was already taken — the dedup, enforced by the unique
        index, not by a read-then-write race."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO proactive_tasks"
                " (skill, label, status, opportunity_key, opportunity_enc, day)"
                " VALUES ($1, $2, 'pending', $3, $4, $5)"
                " ON CONFLICT (skill, opportunity_key) DO NOTHING"
                " RETURNING id, created_at",
                skill,
                label,
                key,
                b"",  # placeholder until the id exists; replaced below, same transaction
                day,
            )
            if row is None:
                return None
            blob = self._cipher.encrypt_json(opportunity, aad=_aad("opportunity_enc", row["id"]))
            await conn.execute(
                "UPDATE proactive_tasks SET opportunity_enc = $1 WHERE id = $2",
                blob,
                row["id"],
            )
        await self._audit.append(
            actor=actor,
            tool_name="proactive_task",
            decision="info",
            params={"skill": skill, "label": label, "key": key},
            outcome="opportunity claimed as a pending task",
        )
        return ProactiveTask(
            id=row["id"],
            skill=skill,
            label=label,
            status=PENDING,
            opportunity_key=key,
            opportunity=dict(opportunity),
            plan=None,
            result=None,
            feedback=None,
            feedback_note=None,
            provider=None,
            day=day,
            created_at=row["created_at"],
            updated_at=row["created_at"],
        )

    async def pending(self) -> list[ProactiveTask]:
        """The queue — claimed but not yet settled, oldest first. After a
        crash these are the re-armed rows; during a cycle they are the ones
        about to run."""
        rows = await self._pool.fetch(
            "SELECT * FROM proactive_tasks WHERE status = 'pending' ORDER BY id"
        )
        return [self._to_task(r) for r in rows]

    async def requeue_running(self) -> int:
        """Boot re-arm: rows a crashed boot left 'running' go back to
        pending, so the next cycle retries them instead of stranding them
        (their keys are taken, so detection can never re-claim them)."""
        rows = await self._pool.fetch(
            "UPDATE proactive_tasks SET status = 'pending', updated_at = now()"
            " WHERE status = 'running' RETURNING id"
        )
        return len(rows)

    async def mark_running(self, task_id: int) -> None:
        await self._mark(task_id, RUNNING)

    async def set_plan(self, task_id: int, plan: dict[str, Any]) -> None:
        """Store what the skill decided to do — encrypted, on the row,
        before the run starts, so a crash mid-run leaves the plan readable."""
        await self._pool.execute(
            "UPDATE proactive_tasks SET plan_enc = $1, updated_at = now() WHERE id = $2",
            self._cipher.encrypt_json(plan, aad=_aad("plan_enc", task_id)),
            task_id,
        )

    async def finish(
        self, task_id: int, result: str, provider: str = "", actor: str = "worker"
    ) -> None:
        await self._pool.execute(
            "UPDATE proactive_tasks SET status = 'done', result_enc = $1,"
            " provider = $2, updated_at = now() WHERE id = $3",
            self._cipher.encrypt_text(result, aad=_aad("result_enc", task_id)),
            provider or None,
            task_id,
        )
        await self._audit.append(
            actor=actor,
            tool_name="proactive_task",
            decision="info",
            params={"task_id": task_id},
            outcome="task done",
        )

    async def fail(self, task_id: int, error: str, actor: str = "worker") -> None:
        """A run that didn't produce a result — a crash, a refusal, a
        provider that couldn't take the job. The error text is stored the
        same way a result is, so the row always says what happened."""
        await self._pool.execute(
            "UPDATE proactive_tasks SET status = 'failed', result_enc = $1,"
            " updated_at = now() WHERE id = $2",
            self._cipher.encrypt_text(error, aad=_aad("result_enc", task_id)),
            task_id,
        )
        await self._audit.append(
            actor=actor,
            tool_name="proactive_task",
            decision="info",
            params={"task_id": task_id},
            outcome="task failed",
        )

    async def link_approval(self, task_id: int, approval_id: int) -> None:
        """Remember which parked action this task's Approve button carries
        out. The link lands in the plan blob — the row's own 'what the
        skill decided' — so it is encrypted and row-bound like everything
        else, and no new column names an approval on a task row."""
        row = await self._pool.fetchrow(
            "SELECT plan_enc FROM proactive_tasks WHERE id = $1", task_id
        )
        if row is None:
            return
        plan: dict[str, Any] = {}
        if row["plan_enc"]:
            plan = self._cipher.decrypt_json(row["plan_enc"], aad=_aad("plan_enc", task_id))
        plan["approval_id"] = approval_id
        await self._pool.execute(
            "UPDATE proactive_tasks SET plan_enc = $1, updated_at = now() WHERE id = $2",
            self._cipher.encrypt_json(plan, aad=_aad("plan_enc", task_id)),
            task_id,
        )

    async def record_feedback(
        self, task_id: int, feedback: str, note: str = "", actor: str = "owner"
    ) -> None:
        """The one-tap verdict on a finished task — approved, revised, or
        discarded, with an optional reason. The reason is encrypted; the
        verdict itself is plain, the same trade the status column makes."""
        if feedback not in (APPROVED, REVISED, DISCARDED):
            raise ValueError(f"unknown feedback {feedback!r}")
        note_blob = (
            self._cipher.encrypt_text(note, aad=_aad("feedback_note_enc", task_id))
            if note
            else None
        )
        await self._pool.execute(
            "UPDATE proactive_tasks SET feedback = $1, feedback_note_enc = $2,"
            " updated_at = now() WHERE id = $3",
            feedback,
            note_blob,
            task_id,
        )
        await self._audit.append(
            actor=actor,
            tool_name="proactive_task",
            decision="info",
            params={"task_id": task_id, "feedback": feedback},
            outcome="task feedback recorded",
        )

    async def feedback_weights(self, days: int = 30) -> dict[str, float]:
        """Per-skill selection bias, derived — never stored. Each approval
        in the window boosts a skill's scores by 15%, each discard decays
        them by the same, clamped to a quarter and to double, so no amount
        of feedback can silence a skill forever or crown one absolutely.
        The arithmetic is deliberately this simple: it is a deterministic
        tie-breaker, not a learned model."""
        rows = await self._pool.fetch(
            "SELECT skill, feedback, COUNT(*) AS n FROM proactive_tasks"
            " WHERE feedback IS NOT NULL"
            " AND updated_at >= now() - make_interval(days => $1)"
            " GROUP BY skill, feedback",
            days,
        )
        deltas: dict[str, float] = {}
        for row in rows:
            sign = 1 if row["feedback"] == APPROVED else (-1 if row["feedback"] == DISCARDED else 0)
            deltas[row["skill"]] = deltas.get(row["skill"], 0.0) + sign * int(row["n"])
        return {skill: min(2.0, max(0.25, 1.0 + 0.15 * delta)) for skill, delta in deltas.items()}

    async def get(self, task_id: int) -> ProactiveTask | None:
        row = await self._pool.fetchrow("SELECT * FROM proactive_tasks WHERE id = $1", task_id)
        return self._to_task(row) if row else None

    async def list(self, limit: int = 50) -> list[ProactiveTask]:
        """Newest first, any status — the panel's view."""
        rows = await self._pool.fetch(
            "SELECT * FROM proactive_tasks ORDER BY id DESC LIMIT $1", limit
        )
        return [self._to_task(r) for r in rows]

    async def _mark(self, task_id: int, status: str) -> None:
        await self._pool.execute(
            "UPDATE proactive_tasks SET status = $1, updated_at = now() WHERE id = $2",
            status,
            task_id,
        )

    def _to_task(self, row: asyncpg.Record) -> ProactiveTask:
        task_id = row["id"]

        def dec(column: str, *, json: bool = False) -> Any:
            blob = row[column]
            if not blob:
                return {} if json else None
            aad = _aad(column, task_id)
            return (
                self._cipher.decrypt_json(blob, aad=aad)
                if json
                else self._cipher.decrypt_text(blob, aad=aad)
            )

        return ProactiveTask(
            id=task_id,
            skill=row["skill"],
            label=row["label"],
            status=row["status"],
            opportunity_key=row["opportunity_key"],
            opportunity=dec("opportunity_enc", json=True),
            plan=dec("plan_enc", json=True),
            result=dec("result_enc"),
            feedback=row["feedback"],
            feedback_note=dec("feedback_note_enc"),
            provider=row["provider"],
            day=row["day"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
