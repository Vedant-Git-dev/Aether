"""The conclusions cache: answers about live state, with a shelf life.

The whole contract is that a state-of-now answer expires — an expired
conclusion must never be served as current. These run against a
real database so the ciphertext-at-rest is proven too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.memory.conclusions import Conclusions
from aether.memory.crypto import Cipher, generate_key_b64

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
SECRET = "AETHER-CONCLUSION-CANARY-77aa"


def _store(db, ttl: int = 120) -> Conclusions:
    return Conclusions(db, Cipher.from_b64(generate_key_b64()), ttl_seconds=ttl)


@pytest.mark.integration
async def test_a_fresh_conclusion_is_served(db) -> None:
    store = _store(db)
    await store.set("now:is any urgent", "nothing urgent", source="live", as_of=NOW)

    found = await store.get_fresh("now:is any urgent", NOW + timedelta(seconds=30))

    assert found is not None
    assert found.value == "nothing urgent"
    assert found.source == "live"
    assert found.as_of_text().startswith("as of ")


@pytest.mark.integration
async def test_an_expired_conclusion_is_not_served(db) -> None:
    store = _store(db, ttl=120)
    await store.set("now:is any urgent", "nothing urgent", as_of=NOW)

    assert await store.get_fresh("now:is any urgent", NOW + timedelta(seconds=121)) is None
    # but the raw value is still on record for an audit
    raw = await store.get("now:is any urgent")
    assert raw is not None and raw.value == "nothing urgent"


@pytest.mark.integration
async def test_a_custom_ttl_is_honoured(db) -> None:
    store = _store(db, ttl=120)
    await store.set("k", "v", as_of=NOW, ttl_seconds=5)

    assert await store.get_fresh("k", NOW + timedelta(seconds=4)) is not None
    assert await store.get_fresh("k", NOW + timedelta(seconds=6)) is None


@pytest.mark.integration
async def test_the_value_is_ciphertext_at_rest(db) -> None:
    store = _store(db)
    await store.set("k", SECRET, as_of=NOW)

    blob = await db.fetchval("SELECT value_enc FROM conclusions")
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob


@pytest.mark.integration
async def test_a_new_value_replaces_the_old_one(db) -> None:
    store = _store(db)
    await store.set("k", "first", as_of=NOW)
    await store.set("k", "second", as_of=NOW + timedelta(seconds=1))

    found = await store.get("k")
    assert found is not None and found.value == "second"
    assert await db.fetchval("SELECT COUNT(*) FROM conclusions") == 1
