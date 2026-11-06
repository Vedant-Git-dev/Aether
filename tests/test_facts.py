"""The facts layer: sealed claims, deterministic consolidation, one ask.

Facts are the durable claims "what's my X" answers from. The body is sealed,
so the store consolidates in process and never asks Postgres to compare a
value. These run against a real database to prove the ciphertext at rest, the
supersede history and the untrusted-content gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.facts import (
    ACTIVE,
    ADDED,
    DISPUTES,
    IGNORED,
    REFUSED,
    SUPERSEDED,
    SUPERSEDES,
    Facts,
    is_reserved_key,
    normalize_key,
)

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
SECRET = "AETHER-FACT-CANARY-9c31"


def _store(db) -> Facts:
    return Facts(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))


def test_key_normalization_and_reserved_domains() -> None:
    assert normalize_key("  Preferred   Meeting Time ") == "preferred meeting time"
    assert is_reserved_key("email") is True
    assert is_reserved_key("home address") is True
    assert is_reserved_key("login password") is True
    assert is_reserved_key("watch criteria") is True
    assert is_reserved_key("favourite colour") is False
    assert is_reserved_key("invoice_due") is False


@pytest.mark.integration
async def test_a_fact_round_trips(db) -> None:
    facts = _store(db)

    outcome = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="timezone",
        value="Asia/Kolkata",
        type="preference",
        provenance=[1],
        now=NOW,
    )

    assert outcome.outcome == ADDED
    stored = await facts.get(outcome.fact.id)
    assert stored is not None
    assert stored.value == "Asia/Kolkata"
    assert stored.subject == "user"
    assert stored.status == ACTIVE
    assert stored.provenance == (1,)


@pytest.mark.integration
async def test_the_body_is_ciphertext_at_rest(db) -> None:
    facts = _store(db)
    outcome = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="private_note",
        value=SECRET,
        provenance=[1],
        now=NOW,
    )
    assert outcome.fact is not None

    blob = await db.fetchval("SELECT body_enc FROM facts")
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob
    columns = {
        row["column_name"]
        for row in await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'facts'"
        )
    }
    assert not {"value", "key", "subject", "provenance"} & columns


@pytest.mark.integration
async def test_provenance_is_mandatory(db) -> None:
    facts = _store(db)

    outcome = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="timezone",
        value="UTC",
        provenance=[],
        now=NOW,
    )

    assert outcome.outcome == REFUSED
    assert await db.fetchval("SELECT COUNT(*) FROM facts") == 0


@pytest.mark.integration
async def test_an_empty_key_or_value_is_refused(db) -> None:
    facts = _store(db)

    assert (
        await facts.consolidate(
            subject_identity_id=None, subject="user", key=" ", value="x", provenance=[1]
        )
    ).outcome == REFUSED
    assert (
        await facts.consolidate(
            subject_identity_id=None, subject="user", key="x", value=" ", provenance=[1]
        )
    ).outcome == REFUSED
    assert await db.fetchval("SELECT COUNT(*) FROM facts") == 0


@pytest.mark.integration
async def test_the_same_claim_twice_is_ignored(db) -> None:
    facts = _store(db)
    first = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="timezone",
        value="Asia/Kolkata",
        provenance=[1],
        now=NOW,
    )

    second = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="timezone",
        value="Asia/Kolkata",
        provenance=[2],
        now=NOW,
    )

    assert first.outcome == ADDED
    assert second.outcome == IGNORED
    assert second.fact is not None and second.fact.id == first.fact.id
    assert await db.fetchval("SELECT COUNT(*) FROM facts") == 1


@pytest.mark.integration
async def test_a_new_value_supersedes_without_overwriting(db) -> None:
    facts = _store(db)
    old = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="meeting_time",
        value="10:00",
        type="preference",
        provenance=[1],
        now=NOW,
    )
    new = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="meeting_time",
        value="11:00",
        type="preference",
        provenance=[2],
        now=NOW + timedelta(hours=1),
    )

    assert new.outcome == SUPERSEDES
    active = await facts.active(now=NOW + timedelta(hours=2))
    assert [fact.value for fact in active] == ["11:00"]
    # the old row survives, ended rather than rewritten
    previous = await facts.get(old.fact.id)
    assert previous is not None
    assert previous.status == SUPERSEDED
    assert previous.value == "10:00"
    assert previous.valid_until == NOW + timedelta(hours=1)


@pytest.mark.integration
async def test_an_untrusted_reserved_claim_is_disputed_not_active(db) -> None:
    facts = _store(db)

    outcome = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="email_address",
        value="attacker@example.com",
        type="contact",
        provenance=[7],
        trusted=False,
        now=NOW,
    )

    assert outcome.outcome == DISPUTES
    assert outcome.fact is not None and outcome.fact.status == "disputed"
    assert await facts.active(now=NOW) == []


@pytest.mark.integration
async def test_an_untrusted_ordinary_claim_can_still_be_recalled(db) -> None:
    facts = _store(db)

    outcome = await facts.consolidate(
        subject_identity_id=None,
        subject="Rahul",
        key="invoice_due",
        value="Friday",
        provenance=[7],
        trusted=False,
        now=NOW,
    )

    assert outcome.outcome in (ADDED, SUPERSEDES)
    assert len(await facts.active(now=NOW)) == 1


@pytest.mark.integration
async def test_an_external_conflict_is_disputed_rather_than_applied(db) -> None:
    facts = _store(db)
    await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="preferred_meeting_time",
        value="10:00",
        type="preference",
        provenance=[1],
        now=NOW,
    )

    external = await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="preferred_meeting_time",
        value="14:00",
        type="preference",
        provenance=[2],
        trusted=False,
        now=NOW,
    )

    assert external.outcome == DISPUTES
    active = await facts.active(now=NOW)
    assert [fact.value for fact in active] == ["10:00"]


@pytest.mark.integration
async def test_a_disputed_claim_is_asked_about_once(db) -> None:
    facts = _store(db)
    await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="password",
        value="hunter2",
        provenance=[7],
        trusted=False,
        now=NOW,
    )

    pending = await facts.pending_questions(now=NOW)
    assert len(pending) == 1
    assert "hunter2" in facts.question_text(pending[0])

    await facts.mark_asked([pending[0].id])

    assert await facts.pending_questions(now=NOW) == []
    # the value is still held, just no longer re-asked
    assert (await facts.get(pending[0].id)).asked is True


@pytest.mark.integration
async def test_two_subjects_with_the_same_key_do_not_collide(db) -> None:
    facts = _store(db)
    await facts.consolidate(
        subject_identity_id=None,
        subject="Rahul",
        key="birthday",
        value="3 November",
        provenance=[1],
        now=NOW,
    )
    await facts.consolidate(
        subject_identity_id=None,
        subject="Anita",
        key="birthday",
        value="7 May",
        provenance=[2],
        now=NOW,
    )

    values = sorted(fact.value for fact in await facts.active(now=NOW))
    assert values == ["3 November", "7 May"]


@pytest.mark.integration
async def test_a_fact_outside_its_validity_window_is_not_active(db) -> None:
    facts = _store(db)
    await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="summer_plan",
        value="goa",
        provenance=[1],
        valid_from=NOW - timedelta(days=1),
        now=NOW,
    )
    # a second claim for the same key ends the first
    await facts.consolidate(
        subject_identity_id=None,
        subject="user",
        key="summer_plan",
        value="manali",
        provenance=[2],
        now=NOW,
    )

    active = await facts.active(now=NOW + timedelta(seconds=1))
    assert [fact.value for fact in active] == ["manali"]
