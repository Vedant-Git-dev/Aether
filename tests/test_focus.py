"""The focus object: one sealed working set per chat.

The store is database-backed, so these run against a real database and prove
the round trip, the ciphertext-at-rest, the key-version binding and the
bounded shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.focus import MAX_RECENT_ENTITIES, Focus

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
CHAT = "web:user-1"
SECRET = "AETHER-FOCUS-CANARY-9c12"


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


@pytest.mark.integration
async def test_the_focus_object_round_trips(db) -> None:
    focus = Focus(db, _cipher())

    await focus.record_turn(
        CHAT,
        entities=[3, 1],
        offer={"text": "Send it to Ada?", "options": ["yes", "no"], "at": NOW.isoformat()},
        open_task={"commitment_id": 5, "summary": "send the invoice", "due_at": None},
        attachment={"media_id": 9, "filename": "deck.pdf", "event_id": 42},
    )

    state = await focus.get(CHAT)
    assert state is not None
    assert state.recent_entities == [3, 1]
    assert state.last_offer is not None and state.last_offer["text"] == "Send it to Ada?"
    assert state.open_task is not None and state.open_task["commitment_id"] == 5
    assert state.last_attachment is not None and state.last_attachment["filename"] == "deck.pdf"


@pytest.mark.integration
async def test_a_turn_without_an_attachment_does_not_erase_it(db) -> None:
    focus = Focus(db, _cipher())
    await focus.record_turn(CHAT, attachment={"media_id": 1, "filename": "a.png", "event_id": 1})

    await focus.record_turn(CHAT, entities=[2])

    state = await focus.get(CHAT)
    assert state is not None
    assert state.last_attachment is not None and state.last_attachment["media_id"] == 1


@pytest.mark.integration
async def test_the_focus_object_is_bounded_to_eight_entities(db) -> None:
    focus = Focus(db, _cipher())

    await focus.record_turn(CHAT, entities=list(range(1, 20)))

    state = await focus.get(CHAT)
    assert state is not None
    assert len(state.recent_entities) == MAX_RECENT_ENTITIES
    # newest first, so the tail of the list was dropped
    assert state.recent_entities[0] == 1


@pytest.mark.integration
async def test_the_state_is_ciphertext_at_rest(db) -> None:
    focus = Focus(db, _cipher())
    await focus.record_turn(CHAT, offer={"text": SECRET, "options": [], "at": NOW.isoformat()})

    blob = await db.fetchval("SELECT state_enc FROM focus")
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob
    columns = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'focus'"
    )
    assert "state" not in {row["column_name"] for row in columns}


@pytest.mark.integration
async def test_a_key_version_mismatch_reads_as_empty(db) -> None:
    key = generate_key_b64()
    writer = Cipher.from_b64(key, key_id="k1")
    reader = Cipher.from_b64(key, key_id="k2")
    focus = Focus(db, writer)
    await focus.record_turn(CHAT, entities=[1])

    # the envelope names k1, which the reader does not hold — fail closed
    assert await Focus(db, reader).get(CHAT) is None


@pytest.mark.integration
async def test_an_unreadable_state_degrades_to_none(db) -> None:
    focus = Focus(db, _cipher())
    await focus.record_turn(CHAT, entities=[1])
    await db.execute("UPDATE focus SET state_enc = $1", b"not-a-sealed-envelope")

    assert await focus.get(CHAT) is None
