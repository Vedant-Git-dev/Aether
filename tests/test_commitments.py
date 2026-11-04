"""The commitments store: sealed rows, in-memory ordering, honest refusals.

Commitments are what the user owes and what they are owed. The body and
the due time are sealed, which is why the open set is read once and ordered
here rather than in SQL — so these run against a real database to prove
both the ordering and the ciphertext-at-rest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.authz.audit import AuditLog
from aether.memory.commitments import DONE, OPEN, Commitments
from aether.memory.crypto import Cipher, generate_key_b64

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
SECRET = "AETHER-COMMITMENT-CANARY-4b71"


def _store(db) -> Commitments:
    return Commitments(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))


@pytest.mark.integration
async def test_a_commitment_round_trips(db) -> None:
    commitments = _store(db)

    stored = await commitments.add(
        kind="promise",
        text="send the October invoice",
        due_at=NOW + timedelta(hours=2),
        due_phrase="by EOD",
    )

    assert stored is not None
    fetched = await commitments.get(stored.id)
    assert fetched is not None
    assert fetched.text == "send the October invoice"
    assert fetched.kind == "promise"
    assert fetched.status == OPEN
    assert fetched.due_at == NOW + timedelta(hours=2)


@pytest.mark.integration
async def test_the_body_is_ciphertext_at_rest(db) -> None:
    commitments = _store(db)
    await commitments.add(kind="task", text=SECRET)

    blob = await db.fetchval("SELECT body_enc FROM commitments")
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob
    # and the plaintext column does not exist to leak through
    columns = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'commitments'"
    )
    assert "text" not in {row["column_name"] for row in columns}


@pytest.mark.integration
async def test_an_empty_commitment_is_refused_not_stored(db) -> None:
    commitments = _store(db)

    assert await commitments.add(kind="task", text="   ") is None
    assert await db.fetchval("SELECT COUNT(*) FROM commitments") == 0


@pytest.mark.integration
async def test_an_unknown_kind_falls_back_to_task(db) -> None:
    commitments = _store(db)

    stored = await commitments.add(kind="vibe", text="something")

    assert stored is not None and stored.kind == "task"


@pytest.mark.integration
async def test_the_open_set_is_ordered_by_due_time_with_undated_last(db) -> None:
    commitments = _store(db)
    await commitments.add(kind="task", text="no deadline")
    await commitments.add(kind="task", text="later", due_at=NOW + timedelta(days=2))
    await commitments.add(kind="task", text="sooner", due_at=NOW + timedelta(hours=1))

    order = [c.text for c in await commitments.list_open()]

    # "no deadline" is not "due now" — it sorts behind everything dated
    assert order == ["sooner", "later", "no deadline"]


@pytest.mark.integration
async def test_resolving_closes_a_commitment_and_keeps_its_words(db) -> None:
    commitments = _store(db)
    stored = await commitments.add(kind="task", text="file the taxes")
    assert stored is not None

    assert await commitments.resolve(stored.id) is True
    assert await commitments.list_open() == []

    closed = await commitments.get(stored.id)
    assert closed is not None
    assert closed.status == DONE
    assert closed.text == "file the taxes"  # the promise survives being done


@pytest.mark.integration
async def test_resolving_twice_is_a_no_op(db) -> None:
    commitments = _store(db)
    stored = await commitments.add(kind="task", text="x")
    assert stored is not None

    assert await commitments.resolve(stored.id) is True
    assert await commitments.resolve(stored.id) is False


@pytest.mark.integration
async def test_an_unknown_status_is_refused(db) -> None:
    commitments = _store(db)
    stored = await commitments.add(kind="task", text="x")
    assert stored is not None

    with pytest.raises(ValueError):
        await commitments.resolve(stored.id, "nearly done")


@pytest.mark.integration
async def test_add_many_survives_a_bad_item(db) -> None:
    commitments = _store(db)

    stored = await commitments.add_many(
        [
            {"kind": "task", "text": "the good one"},
            {"kind": "task", "text": ""},  # refused, not fatal
            {"kind": "request", "text": "and another", "due_at": NOW},
        ],
        source_event_id=None,
    )

    assert [c.text for c in stored] == ["the good one", "and another"]
    assert await db.fetchval("SELECT COUNT(*) FROM commitments") == 2
