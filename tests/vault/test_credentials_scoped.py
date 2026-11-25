"""Scoped credentials storage: config overrides, app secrets, provider
connections/OAuth state/sync cursors, and agent settings, against scratch
Postgres with the tests-only fake vault.

The provider path bypasses the legacy token keyring entirely under a scope:
one task's key seals every field, and `key_id` becomes an informational
label rather than a key selector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fake_vault import FakeVault

from aether.agent.settings import AgentSettings
from aether.config_store import ConfigOverrides
from aether.providers.store import ProviderStore
from aether.providers.types import ProviderConnection
from aether.secret_store import SecretStore
from aether.vault.errors import VaultProtocolError
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import VaultClosed, VaultLocked

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
CREDS = [("credentials", 1)]
MEMORY = [("memory", 1)]


class _NoSqlPool:
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


async def test_scoped_credentials_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "panel_write", [("operations", 1)]) as scope:
        overrides = ConfigOverrides.scoped(db, scope)
        await overrides.upsert("llm.model", "gemini-flash")
        assert await overrides.load() == {"llm.model": "gemini-flash"}
        raw = await db.fetchval("SELECT value_enc FROM config_overrides WHERE path = 'llm.model'")
        assert raw.startswith(AE2_MAGIC)

    async with vault.acquire(TASK, "credential_write", CREDS) as scope:
        secrets_store = SecretStore.scoped(db, scope)
        await secrets_store.set("TELEGRAM_BOT_TOKEN", "123:abc")
        assert await secrets_store.get("TELEGRAM_BOT_TOKEN") == "123:abc"
        assert await secrets_store.names() == {"TELEGRAM_BOT_TOKEN"}

        providers = ProviderStore.scoped(db, scope)
        connection = ProviderConnection(
            provider="google",
            auth_mode="oauth",
            access_token="access-1",
            refresh_token="refresh-1",
            scopes="mail",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        saved = await providers.upsert(connection)
        assert saved.key_id == "v1"
        row = await db.fetchrow(
            "SELECT access_enc, refresh_enc, key_version FROM provider_connections"
            " WHERE provider = 'google'"
        )
        assert row["access_enc"].startswith(AE2_MAGIC)
        assert row["key_version"] == "v1"
        back = await providers.get("google")
        assert back is not None
        assert back.access_token == "access-1" and back.refresh_token == "refresh-1"
        # no token keyring is configured on the scoped path at all
        assert providers._keyring is None

        state_hash = "a" * 64
        await providers.create_state(
            state_hash=state_hash,
            provider="google",
            app="mail",
            scopes="mail",
            code_verifier="verifier-xyz",
            redirect_uri="https://example.test/cb",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        state = await providers.consume_state(state_hash)
        assert state is not None and state.code_verifier == "verifier-xyz"

        await providers.set_sync("google", "mail", "cursor", "12345")
        assert await providers.get_sync("google", "mail", "cursor") == "12345"


async def test_scoped_agent_settings_round_trip(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("operations", 1)]) as scope:
        settings = AgentSettings.scoped(db, scope)
        await settings.set_personality("be concise")
        assert await settings.get_personality() == "be concise"
        raw = await db.fetchval("SELECT personality_enc FROM agent_settings WHERE id = 1")
        assert raw.startswith(AE2_MAGIC)


async def test_scoped_credentials_denied_issue_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", MEMORY) as scope:
        with pytest.raises(VaultLocked):
            await ConfigOverrides.scoped(pool, scope).load()
        with pytest.raises(VaultLocked):
            await SecretStore.scoped(pool, scope).get("TOKEN")
        with pytest.raises(VaultLocked):
            await ProviderStore.scoped(pool, scope).list()
    async with vault.acquire(TASK, "credential_write", CREDS) as scope:
        scoped_secrets = SecretStore.scoped(pool, scope)
    with pytest.raises(VaultClosed):
        await scoped_secrets.names()


async def test_scoped_provider_corrupt_token_fails_closed(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "credential_write", CREDS) as scope:
        providers = ProviderStore.scoped(db, scope)
        await providers.upsert(
            ProviderConnection(
                provider="google",
                auth_mode="oauth",
                access_token="access-1",
                scopes="mail",
            )
        )
        blob = await db.fetchval(
            "SELECT access_enc FROM provider_connections WHERE provider = 'google'"
        )
        broken = bytearray(blob)
        broken[-1] ^= 0xFF
        await db.execute(
            "UPDATE provider_connections SET access_enc = $1 WHERE provider = 'google'",
            bytes(broken),
        )
        with pytest.raises(VaultProtocolError):
            await providers.get("google")
