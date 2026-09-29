"""AgentSettings store tests — behind the integration marker (real Postgres
and real encryption round-trips; no unit-testable logic lives here)."""

from __future__ import annotations

import pytest

from aether.agent.settings import AgentSettings
from aether.memory.crypto import Cipher, generate_key_b64


def _make_settings(db) -> AgentSettings:
    return AgentSettings(db, Cipher.from_b64(generate_key_b64()))


@pytest.mark.integration
async def test_personality_is_empty_before_anything_is_saved(db) -> None:
    settings = _make_settings(db)
    assert await settings.get_personality() == ""


@pytest.mark.integration
async def test_set_then_get_round_trips(db) -> None:
    settings = _make_settings(db)
    await settings.set_personality("Be blunt. Skip the pleasantries.")
    assert await settings.get_personality() == "Be blunt. Skip the pleasantries."


@pytest.mark.integration
async def test_set_again_overwrites_the_single_row(db) -> None:
    settings = _make_settings(db)
    await settings.set_personality("first draft")
    await settings.set_personality("second draft")
    assert await settings.get_personality() == "second draft"
    count = await db.fetchval("SELECT count(*) FROM agent_settings")
    assert count == 1


@pytest.mark.integration
async def test_what_is_on_disk_is_ciphertext_not_the_text(db) -> None:
    settings = _make_settings(db)
    await settings.set_personality("a private instruction nobody else should read")
    raw = await db.fetchval("SELECT personality_enc FROM agent_settings WHERE id = 1")
    assert b"private instruction" not in raw


@pytest.mark.integration
async def test_a_different_key_cannot_decrypt_another_owners_row(db) -> None:
    settings = _make_settings(db)
    await settings.set_personality("only readable with the right key")
    wrong_key = AgentSettings(db, Cipher.from_b64(generate_key_b64()))
    assert await wrong_key.get_personality() == ""  # decrypt fails closed, not loudly
