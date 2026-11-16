import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.held_notes import DbHeldNotes, MemoryHeldNotes


async def test_memory_store_returns_notes_in_order_and_releases_them() -> None:
    store = MemoryHeldNotes()
    await store.hold("first")
    await store.hold("second")

    notes = await store.pending()
    assert [note.text for note in notes] == ["first", "second"]

    await store.release([notes[0].id])
    assert [note.text for note in await store.pending()] == ["second"]


@pytest.mark.integration
async def test_db_store_seals_the_text_and_survives_a_new_object(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    await DbHeldNotes(db, cipher).hold("the surprise party is at 8")

    raw = await db.fetchval("SELECT body_enc FROM held_notes")
    assert b"surprise" not in bytes(raw)

    reopened = DbHeldNotes(db, cipher)
    notes = await reopened.pending()
    assert [note.text for note in notes] == ["the surprise party is at 8"]

    await reopened.release([note.id for note in notes])
    assert await reopened.pending() == []


@pytest.mark.integration
async def test_db_store_releases_a_row_it_cannot_open_without_sending_it(db) -> None:
    await DbHeldNotes(db, Cipher.from_b64(generate_key_b64())).hold("old key")

    notes = await DbHeldNotes(db, Cipher.from_b64(generate_key_b64())).pending()
    assert [note.text for note in notes] == [""]
