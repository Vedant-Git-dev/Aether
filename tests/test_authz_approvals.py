"""Approvals store tests — all behind the integration marker (real Postgres
and real encryption round-trips; no unit-testable logic lives here)."""

from __future__ import annotations

import pytest

from aether.authz.approvals import APPROVED, DENIED, PENDING, Approvals
from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, CryptoError, generate_key_b64


async def _make_approvals(db, ttl_hours: float = 24.0) -> Approvals:
    cipher = Cipher.from_b64(generate_key_b64())
    return Approvals(db, cipher, AuditLog(db), ttl_hours=ttl_hours)


@pytest.mark.integration
async def test_create_parks_the_call_encrypted(db) -> None:
    approvals = await _make_approvals(db)
    approval = await approvals.create(
        tool_name="telegram__send_message",
        params={"chat_id": 123, "text": "happy birthday!"},
        rules_matched="builtin:risky",
    )
    assert approval.status == PENDING
    assert approval.params == {"chat_id": 123, "text": "happy birthday!"}

    # what's on disk is ciphertext, not the params
    raw = await db.fetchval(
        "SELECT params_enc FROM pending_approvals WHERE id = $1", approval.id
    )
    assert b"happy birthday" not in raw

    # and it comes back through the normal read path
    fetched = await approvals.get(approval.id)
    assert fetched is not None
    assert fetched.params == {"chat_id": 123, "text": "happy birthday!"}


@pytest.mark.integration
async def test_ciphertext_is_bound_to_its_row(db) -> None:
    approvals = await _make_approvals(db)
    a = await approvals.create(tool_name="t__one", params={"secret": "one"})
    b = await approvals.create(tool_name="t__two", params={"secret": "two"})

    blob_a = await db.fetchval("SELECT params_enc FROM pending_approvals WHERE id = $1", a.id)
    with pytest.raises(CryptoError):
        # AAD includes the record id — swapping blobs between rows fails
        approvals._cipher.decrypt_text(blob_a, aad=f"pending_approvals:params_enc:{b.id}")


@pytest.mark.integration
async def test_approve_and_deny_decide_pending_rows(db) -> None:
    approvals = await _make_approvals(db)
    parked = await approvals.create(tool_name="mail__send_email", params={"to": "a@b.c"})
    denied = await approvals.create(tool_name="payments__pay", params={"amount": 5})

    approved = await approvals.decide(parked.id, APPROVED)
    assert approved is not None
    assert approved.status == APPROVED
    assert approved.decided_by == "user"
    assert approved.decided_at is not None
    assert approved.params == {"to": "a@b.c"}

    refused = await approvals.decide(denied.id, DENIED, decided_by="user")
    assert refused is not None
    assert refused.status == DENIED

    # already decided — deciding again changes nothing
    assert await approvals.decide(parked.id, DENIED) is None


@pytest.mark.integration
async def test_invalid_decision_value_rejected(db) -> None:
    approvals = await _make_approvals(db)
    with pytest.raises(ValueError, match="must be"):
        await approvals.decide(1, "maybe")


@pytest.mark.integration
async def test_list_pending_returns_only_pending(db) -> None:
    approvals = await _make_approvals(db)
    keep = await approvals.create(tool_name="t__keep", params={"a": 1})
    await approvals.create(tool_name="t__gone", params={"a": 2})
    await approvals.decide(keep.id + 1, DENIED)  # the second row

    pending = await approvals.list_pending()
    assert [p.id for p in pending] == [keep.id]
    assert pending[0].tool_name == "t__keep"


@pytest.mark.integration
async def test_expire_overdue_flips_and_audits(db) -> None:
    approvals = await _make_approvals(db, ttl_hours=0.0)  # expires immediately
    stale = await approvals.create(tool_name="t__stale", params={"x": 1})

    expired = await approvals.expire_overdue()
    assert expired == 1
    assert (await approvals.get(stale.id)).status == "expired"

    # an expired approval can no longer be decided
    assert await approvals.decide(stale.id, APPROVED) is None

    # the expiry landed in the audit chain
    audit_rows = await db.fetch("SELECT * FROM audit_log WHERE outcome = 'approval expired'")
    assert len(audit_rows) == 1


@pytest.mark.integration
async def test_mark_executed_flips_approved_rows_only(db) -> None:
    approvals = await _make_approvals(db)
    parked = await approvals.create(tool_name="t__x", params={"x": 1})
    fresh = await approvals.create(tool_name="t__y", params={"y": 2})

    # not approved yet — no-op
    await approvals.mark_executed(parked.id)
    assert (await approvals.get(parked.id)).status == PENDING

    await approvals.decide(fresh.id, APPROVED)
    await approvals.mark_executed(fresh.id)
    assert (await approvals.get(fresh.id)).status == "executed"


@pytest.mark.integration
async def test_every_step_is_audited(db) -> None:
    approvals = await _make_approvals(db)
    parked = await approvals.create(
        tool_name="mail__send_email", params={"to": "a@b.c"}, rules_matched="builtin:risky"
    )
    await approvals.decide(parked.id, APPROVED)
    await approvals.mark_executed(parked.id)

    rows = await db.fetch(
        "SELECT decision, outcome FROM audit_log ORDER BY seq"
    )
    assert [r["decision"] for r in rows] == ["approve", "allow", "info"]
    assert [r["outcome"] for r in rows] == ["parked for approval", "approved by user", "executed after approval"]

    # the chain across all those appends still verifies
    verification = await AuditLog(db).verify_chain()
    assert verification.ok is True
    assert verification.entries == 3
