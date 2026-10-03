"""The paste store — the roundtrip through a fake pool is real
AES-256-GCM with name-bound AAD, the OAuthTokenStore pattern.

These pin the store's contract: a pasted value never sits in the row in
the clear, the same name upserts instead of erroring, names() answers
names only, and a blob swapped between rows decrypts as nothing — the AAD
binds each ciphertext to its name, so a leaked ciphertext from one key
can't masquerade as another.
"""

from __future__ import annotations

import pytest

from aether.memory.crypto import Cipher, CryptoError
from aether.secret_store import SecretStore

KEY = "1LGa0gf0yStM18BtqZU7DmlPZFNCkSA5VjWAvPzY7sA="


class FakePool:
    """asyncpg's surface over an in-memory table — the roundtrip through it
    is real AES-256-GCM with name-bound AAD."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def execute(self, sql: str, *args) -> None:
        assert sql.lstrip().startswith("INSERT")  # one-step upsert, save shape
        name, value_enc = args
        self.rows[name] = {"name": name, "value_enc": value_enc}

    async def fetchrow(self, sql: str, name: str) -> dict | None:
        return self.rows.get(name)

    async def fetch(self, sql: str) -> list[dict]:
        return list(self.rows.values())


def _store() -> tuple[FakePool, SecretStore]:
    pool = FakePool()
    return pool, SecretStore(pool, Cipher.from_b64(KEY))


async def test_a_pasted_key_roundtrips_encrypted() -> None:
    pool, store = _store()
    await store.set("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_secret")

    row = pool.rows["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert b"ghp_secret" not in row["value_enc"]  # never in the row in the clear
    assert await store.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "ghp_secret"
    assert await store.get("NEVER_PASTED") is None


async def test_the_same_name_upserts_instead_of_erroring() -> None:
    pool, store = _store()
    await store.set("TELEGRAM_BOT_TOKEN", "old-token")
    await store.set("TELEGRAM_BOT_TOKEN", "new-token")  # the re-paste wins

    assert len(pool.rows) == 1  # one row per name — the upsert overwrote
    assert await store.get("TELEGRAM_BOT_TOKEN") == "new-token"


async def test_names_answers_names_only() -> None:
    _, store = _store()
    await store.set("SLACK_BOT_TOKEN", "xoxb-secret")
    await store.set("SLACK_APP_TOKEN", "xapp-secret")

    names = await store.names()
    assert names == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}  # values never ride along


async def test_a_swapped_blob_fails_to_masquerade() -> None:
    """AAD binds each ciphertext to its name — a token ciphertext pasted
    into another key's row decrypts as nothing, so a leaked blob can't be
    replayed under a different name."""
    pool, store = _store()
    await store.set("SLACK_BOT_TOKEN", "xoxb-real")
    await store.set("SLACK_APP_TOKEN", "xapp-real")
    pool.rows["SLACK_BOT_TOKEN"]["value_enc"] = pool.rows["SLACK_APP_TOKEN"]["value_enc"]
    with pytest.raises(CryptoError):
        await store.get("SLACK_BOT_TOKEN")
