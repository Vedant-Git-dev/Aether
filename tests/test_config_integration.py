"""Chat-made config integration tests — real Postgres, real encryption.

These live behind the integration marker (AETHER_TEST_DATABASE_URL): the
db fixture gives each test a migrated, truncated scratch database — never
the live one. What's pinned here is the durability half of the feature:
rows survive the process, values are ciphertext at rest bound to their
path, and a fresh boot rebuilds the live config from them (the Render
story end to end).
"""

from __future__ import annotations

import pytest

from aether.config import AppConfig, ContactRule, Settings
from aether.config_store import ConfigManager, ConfigOverrides
from aether.memory.crypto import Cipher, generate_key_b64


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


@pytest.mark.integration
async def test_store_roundtrips_overwrites_and_deletes(db) -> None:
    store = ConfigOverrides(db, _cipher())

    await store.upsert("agent.tick_seconds", 10)
    assert await store.load() == {"agent.tick_seconds": 10}

    await store.upsert("agent.tick_seconds", 5)  # one row per path, latest wins
    await store.upsert("llm.model", "claude-sonnet-5")
    assert await store.load() == {"agent.tick_seconds": 5, "llm.model": "claude-sonnet-5"}
    assert await db.fetchval("SELECT COUNT(*) FROM config_overrides") == 2

    await store.delete("agent.tick_seconds")
    await store.upsert("mcp_servers.mail.enabled", False)
    await store.delete_prefixed("mcp_servers")  # the list's item rows go together
    assert await store.load() == {"llm.model": "claude-sonnet-5"}


@pytest.mark.integration
async def test_values_are_ciphertext_at_rest(db) -> None:
    store = ConfigOverrides(db, _cipher())
    await store.upsert("llm.model", "claude-sonnet-5")

    blob = await db.fetchval("SELECT value_enc FROM config_overrides WHERE path = 'llm.model'")
    assert b"claude-sonnet-5" not in bytes(blob)  # the setting never sits in the clear


@pytest.mark.integration
async def test_a_wrong_key_fails_closed_not_loud(db) -> None:
    store = ConfigOverrides(db, _cipher())
    await store.upsert("agent.tick_seconds", 10)

    stranger = ConfigOverrides(db, _cipher())  # key rotation drift
    assert await stranger.load() == {}  # skipped with a warning — boot continues


@pytest.mark.integration
async def test_a_blob_swapped_between_paths_fails_to_decrypt(db) -> None:
    cipher = _cipher()
    store = ConfigOverrides(db, cipher)
    await store.upsert("agent.tick_seconds", 10)
    blob = await db.fetchval(
        "SELECT value_enc FROM config_overrides WHERE path = 'agent.tick_seconds'"
    )

    # a row for another path carrying this row's blob — the AAD binds each
    # ciphertext to the path it was written for
    await db.execute(
        "INSERT INTO config_overrides (path, value_enc) VALUES ($1, $2)", "llm.model", blob
    )
    assert await store.load() == {"agent.tick_seconds": 10}  # the swapped row is skipped


@pytest.mark.integration
async def test_bootstrap_merges_rows_onto_a_fresh_config(db) -> None:
    store = ConfigOverrides(db, _cipher())
    await store.upsert("agent.tick_seconds", 10)
    await store.upsert("mcp_servers", [{"name": "mail"}, {"name": "calendar"}])
    await store.upsert("mcp_servers.mail.enabled", False)

    live = AppConfig()
    manager = ConfigManager(store, Settings(_env_file=None), live)
    assert await manager.bootstrap() == 3
    assert live.agent.tick_seconds == 10.0
    assert [s.name for s in live.mcp_servers] == ["mail", "calendar"]
    assert live.mcp_servers[0].enabled is False  # the item row composed after the root


@pytest.mark.integration
async def test_a_chat_set_survives_a_fresh_process(db) -> None:
    cipher = _cipher()  # in production this key comes from .env — stable across boots

    # session 1: config.yaml's values, then chat changes two of them
    live = AppConfig()
    manager = ConfigManager(ConfigOverrides(db, cipher), Settings(_env_file=None), live)
    assert (await manager.set(op="set", path="agent.tick_seconds", value=10)).startswith(
        "agent.tick_seconds set to 10"
    )
    await manager.set(op="add", path="contacts.allowlist", value="telegram @friend")

    # session 2: a fresh process on ephemeral disk — pydantic defaults, the
    # same rows, and the boot merge rebuilds what chat made
    fresh = AppConfig()
    manager2 = ConfigManager(ConfigOverrides(db, cipher), Settings(_env_file=None), fresh)
    assert await manager2.bootstrap() == 2
    assert fresh.agent.tick_seconds == 10.0
    assert fresh.contacts.allowlist == [ContactRule(platform="telegram", handle="@friend")]

    # reset falls back to the yaml snapshot and takes the row with it
    assert (await manager2.set(op="reset", path="agent.tick_seconds")).startswith(
        "agent.tick_seconds reset to config.yaml (30)"
    )
    assert fresh.agent.tick_seconds == 30.0
    store2 = ConfigOverrides(db, cipher)
    assert await store2.load() == {
        "contacts.allowlist": [{"platform": "telegram", "handle": "@friend"}]
    }
