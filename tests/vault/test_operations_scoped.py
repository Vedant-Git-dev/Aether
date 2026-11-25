"""Scoped operations storage: deliveries, alerts, approvals, traces, the
scheduler, proactive tasks, workbench transfers and inbound states, tested
against scratch Postgres with the tests-only fake vault.

Each store keeps its legacy serving path; the scoped path seals through the
task's KeyScope, fails before SQL without the category, and fails closed on
a corrupt body instead of dropping the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fake_vault import FakeVault

from aether.agent.traces import Traces
from aether.alerts.notifications import AlertNotifications
from aether.alerts.spec import AlertSpec
from aether.alerts.store import AlertStore
from aether.authz.approvals import Approvals
from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.deliveries import Deliveries
from aether.memory.inbound import RECEIVED, DbInboundStates
from aether.origin import owner_message
from aether.proactive.store import ProactiveStore
from aether.scheduler.jobs import Scheduler
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import LegacyCiphertextError, VaultClosed, VaultLocked
from aether.workbench.transfers import TransferStore

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
OPS = [("operations", 1)]
MEMORY = [("memory", 1)]


class _NoSqlPool:
    """Any query is a test failure: a denied read must issue zero SQL."""

    async def fetchrow(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetch(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetchval(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def execute(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    def acquire(self) -> None:
        raise AssertionError("a denied read issued SQL")


async def test_scoped_operations_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", OPS) as scope:
        audit = AuditLog(db)

        deliveries = Deliveries.scoped(db, scope)
        delivery = await deliveries.record_queued("telegram", "happy birthday!")
        assert delivery is not None
        row = await db.fetchrow(
            "SELECT body_enc, key_version FROM deliveries WHERE id = $1", delivery.id
        )
        assert row["body_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        back = await deliveries.get(delivery.id)
        assert back is not None and back.body == "happy birthday!"

        traces = Traces.scoped(db, scope)
        trace = await traces.create(kind="turn", label="unread", payload={"reply": "2 unread"})
        assert (await traces.get(trace.id)).payload == {"reply": "2 unread"}

        approvals = Approvals.scoped(db, scope, audit)
        parked = await approvals.create(tool_name="mail__send", params={"to": "a@b.c"})
        assert (await approvals.get(parked.id)).params == {"to": "a@b.c"}

        inbound = DbInboundStates.scoped(db, scope)
        got = await inbound.record("telegram", "chat-1", "m-1")
        assert got is not None and got.status == RECEIVED
        await inbound.mark_failed(got.id, "the model refused")
        assert (await inbound.get(got.id)).reason == "the model refused"

        alerts = AlertStore.scoped(db, scope, audit)
        armed = await alerts.create(
            spec=AlertSpec(title="invoices", scope="mail", contains="invoice"),
            origin=owner_message(),
        )
        assert armed is not None
        spec_row = await db.fetchrow(
            "SELECT spec_enc, origin_enc FROM alerts WHERE id = $1", armed.id
        )
        assert spec_row["spec_enc"].startswith(AE2_MAGIC)
        assert spec_row["origin_enc"].startswith(AE2_MAGIC)
        assert (await alerts.get(armed.id)).spec.contains == "invoice"

        notifications = AlertNotifications.scoped(db, scope)
        assert await notifications.recent() == []

        scheduler = Scheduler.scoped(db, scope, audit)
        action = await scheduler.create(
            label="send later",
            run_at=datetime.now(UTC) + timedelta(minutes=5),
            payload={"tool": "mail__send"},
        )
        assert (await scheduler.get(action.id)).label == "send later"

        proactive = ProactiveStore.scoped(db, scope, audit)
        claimed = await proactive.claim(
            skill="followups",
            key="k1",
            label="reply",
            opportunity={"draft": "hi"},
            day="2026-11-24",
        )
        assert claimed is not None
        await proactive.set_plan(claimed.id, {"step": "send"})
        assert (await proactive.get(claimed.id)).plan == {"step": "send"}

        transfers = TransferStore.scoped(db, scope)
        ticket = await transfers.mint(kind="bundle", payload={"a": 1}, ttl_seconds=60)
        assert await transfers.take_bundle(ticket.token) == {"a": 1}


async def test_scoped_operations_denied_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", MEMORY) as scope:
        audit = AuditLog(pool)
        with pytest.raises(VaultLocked):
            await Deliveries.scoped(pool, scope).record_queued("telegram", "x")
        with pytest.raises(VaultLocked):
            await Traces.scoped(pool, scope).recent()
        with pytest.raises(VaultLocked):
            await Approvals.scoped(pool, scope, audit).list_pending()
        with pytest.raises(VaultLocked):
            await DbInboundStates.scoped(pool, scope).recent()
        with pytest.raises(VaultLocked):
            await AlertStore.scoped(pool, scope, audit).list_active()
        with pytest.raises(VaultLocked):
            await Scheduler.scoped(pool, scope, audit).list()
        with pytest.raises(VaultLocked):
            await ProactiveStore.scoped(pool, scope, audit).list()
        with pytest.raises(VaultLocked):
            await TransferStore.scoped(pool, scope).mint(kind="bundle", payload={}, ttl_seconds=60)
        with pytest.raises(VaultLocked):
            await AlertNotifications.scoped(pool, scope).recent()
    scoped_deliveries = None
    async with vault.acquire(TASK, "turn", OPS) as scope:
        scoped_deliveries = Deliveries.scoped(pool, scope)
    with pytest.raises(VaultClosed):
        await scoped_deliveries.recent()


async def test_scoped_operations_corrupt_body_fails_closed(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", OPS) as scope:
        deliveries = Deliveries.scoped(db, scope)
        delivery = await deliveries.record_queued("telegram", "secret words")
        blob = await db.fetchval("SELECT body_enc FROM deliveries WHERE id = $1", delivery.id)
        broken = bytearray(blob)
        broken[-1] ^= 0xFF
        await db.execute(
            "UPDATE deliveries SET body_enc = $1 WHERE id = $2", bytes(broken), delivery.id
        )
        with pytest.raises(VaultProtocolError):
            await deliveries.get(delivery.id)


async def test_scoped_reads_reject_an_old_plaintext_key_row(db) -> None:
    """Old-data fixture: a row sealed with the legacy serving key is not
    silently opened by a scope; the scoped read reports the mismatch."""
    legacy = Cipher.from_b64(generate_key_b64())
    legacy_deliveries = Deliveries(db, legacy)
    old = await legacy_deliveries.record_queued("telegram", "written before the vault")
    assert old is not None

    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", OPS) as scope:
        scoped = Deliveries.scoped(db, scope)
        with pytest.raises(LegacyCiphertextError):
            await scoped.get(old.id)


async def test_legacy_operations_path_is_unchanged(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    traces = Traces(db, cipher)
    trace = await traces.create(kind="turn", label="legacy", payload={"reply": "ok"})
    raw = await db.fetchval("SELECT trace_enc FROM decision_traces WHERE id = $1", trace.id)
    assert b"ok" not in raw
    assert (await traces.get(trace.id)).label == "legacy"

    approvals = Approvals(db, cipher, audit)
    parked = await approvals.create(tool_name="t__one", params={"secret": "one"})
    assert (await approvals.get(parked.id)).params == {"secret": "one"}
