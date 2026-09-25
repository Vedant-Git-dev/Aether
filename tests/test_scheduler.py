"""Scheduler tests.

Unit: process_due / the worker against a fake store.
Integration: real Postgres — persistence at creation, atomic claims,
restart survival (the point of writing actions to the database at all).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.scheduler import DONE, FAILED, PENDING, RUNNING, Scheduler, SchedulerWorker, process_due
from aether.scheduler.jobs import ScheduledAction
from fakes import FakeSchedStore


def _action(action_id: int, *, due: bool = True, label: str = "ping") -> ScheduledAction:
    now = datetime.now(timezone.utc)
    when = now - timedelta(minutes=1) if due else now + timedelta(hours=1)
    return ScheduledAction(
        id=action_id, label=label, run_at=when, status=PENDING,
        payload={"type": "tool", "tool": "mail__list_messages", "params": {}},
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# unit: process_due + the worker, against the fake store
# ---------------------------------------------------------------------------


async def test_process_due_runs_and_marks_done() -> None:
    store = FakeSchedStore([_action(1), _action(2, due=False)])
    ran: list[int] = []

    async def run(action: ScheduledAction) -> str:
        ran.append(action.id)
        return f"result of {action.id}"

    count = await process_due(store, run)
    assert count == 1  # only the due one
    assert ran == [1]
    assert store.marks == [(1, DONE)]
    assert store.actions[0].status == DONE
    # the future action is untouched
    assert store.actions[1].status == PENDING


async def test_process_due_records_failures_without_stopping() -> None:
    store = FakeSchedStore([_action(1), _action(2, label="boom")])
    seen: list[int] = []

    async def run(action: ScheduledAction) -> str:
        seen.append(action.id)
        if action.label == "boom":
            raise RuntimeError("the connector exploded")
        return "ok"

    count = await process_due(store, run)
    assert count == 2  # the failure didn't stop the second action
    assert seen == [1, 2]
    assert store.marks == [(1, DONE), (2, FAILED)]
    assert store.actions[1].status == FAILED


async def test_the_worker_loops_until_stopped() -> None:
    store = FakeSchedStore([_action(1)])
    ran: list[int] = []

    async def run(action: ScheduledAction) -> str:
        ran.append(action.id)
        return "done"

    worker = SchedulerWorker(store, run, interval=0.05)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not store.marks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert store.marks == [(1, DONE)]
        assert ran == [1]
    finally:
        await worker.stop()

    # stopped: a newly due action is never picked up
    store.actions.append(_action(2))
    await asyncio.sleep(0.2)
    assert ran == [1]


# ---------------------------------------------------------------------------
# integration: the real store
# ---------------------------------------------------------------------------


def _make_scheduler(db) -> Scheduler:
    return Scheduler(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))


@pytest.mark.integration
async def test_create_persists_at_creation_time_encrypted(db) -> None:
    scheduler = _make_scheduler(db)
    when = datetime.now(timezone.utc) + timedelta(hours=6)
    action = await scheduler.create(
        label="send the file",
        run_at=when,
        payload={"type": "tool", "tool": "telegram__send_file",
                 "params": {"path": "report.pdf", "secret note": "for alice only"}},
    )
    assert action.id > 0
    assert action.status == PENDING

    # on disk: ciphertext, not the payload
    raw = await db.fetchval(
        "SELECT payload_enc FROM scheduled_actions WHERE id = $1", action.id
    )
    assert b"for alice only" not in raw

    # and the read path decrypts it back
    fetched = await scheduler.get(action.id)
    assert fetched is not None
    assert fetched.payload["params"]["secret note"] == "for alice only"
    assert fetched.run_at == when


@pytest.mark.integration
async def test_claim_due_is_atomic_and_selective(db) -> None:
    scheduler = _make_scheduler(db)
    due = await scheduler.create(label="due now", run_at=datetime.now(timezone.utc) - timedelta(minutes=5), payload={"x": 1})
    later = await scheduler.create(label="later", run_at=datetime.now(timezone.utc) + timedelta(hours=1), payload={"x": 2})

    claimed = await scheduler.claim_due()
    assert [a.id for a in claimed] == [due.id]
    assert claimed[0].status == RUNNING

    # the claim was the transition: a second claim gets nothing, the
    # future row was never touched
    assert await scheduler.claim_due() == []
    untouched = await scheduler.get(later.id)
    assert untouched.status == PENDING


@pytest.mark.integration
async def test_process_due_completes_and_fails_over_the_db(db) -> None:
    scheduler = _make_scheduler(db)
    ok = await scheduler.create(label="ok", run_at=datetime.now(timezone.utc) - timedelta(minutes=1), payload={"n": 1})
    bad = await scheduler.create(label="bad", run_at=datetime.now(timezone.utc) - timedelta(minutes=1), payload={"n": 2})

    async def run(action: ScheduledAction) -> str:
        if action.label == "bad":
            raise RuntimeError("tool refused")
        return "the result"

    assert await process_due(scheduler, run) == 2
    done = await scheduler.get(ok.id)
    failed = await scheduler.get(bad.id)
    assert done.status == DONE
    assert done.payload == {"n": 1}  # decrypted on the way out of the claim
    assert failed.status == FAILED

    digest = await db.fetchval(
        "SELECT result_digest FROM scheduled_actions WHERE id = $1", ok.id
    )
    assert digest == hashlib.sha256(b"the result").hexdigest()[:16]


@pytest.mark.integration
async def test_overdue_pending_survives_a_restart(db) -> None:
    """The whole point: a row written before a crash is found and run after
    the process comes back up — modeled here as a brand-new Scheduler
    instance over the same database."""
    first = _make_scheduler(db)
    await first.create(
        label="send while I was down",
        run_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        payload={"type": "tool", "tool": "mail__send_message", "params": {"to": "me"}},
    )

    # "restart": nothing carries over but the database
    second = Scheduler(db, first._cipher, AuditLog(db))
    overdue = await second.overdue_pending()
    assert [a.label for a in overdue] == ["send while I was down"]

    ran: list[str] = []

    async def run(action: ScheduledAction) -> str:
        ran.append(action.label)
        return "sent"

    assert await process_due(second, run) == 1
    assert ran == ["send while I was down"]
    assert await second.overdue_pending() == []
