"""Provider token storage and rotation, against a scratch Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.memory.crypto import generate_key_b64
from aether.providers.crypto import TokenKeyring
from aether.providers.rotate_tokens import _rotate_connections, _rotate_states
from aether.providers.store import ProviderStore
from aether.providers.types import ProviderConnection


def _keyring(key_id: str = "v1") -> TokenKeyring:
    return TokenKeyring(active_id=key_id, active_key_b64=generate_key_b64())


@pytest.mark.integration
async def test_provider_store_round_trip(db) -> None:
    store = ProviderStore(db, _keyring())
    connection = ProviderConnection(
        provider="google",
        auth_mode="oauth",
        account_label="me@example.com",
        account_id="acct-1",
        access_token="access-token",
        refresh_token="refresh-token",
        scopes="read write",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
    )
    await store.upsert(connection)

    raw = await db.fetchrow(
        "SELECT access_enc, refresh_enc, key_id FROM provider_connections WHERE provider = 'google'"
    )
    assert raw is not None
    assert raw["key_id"] == "v1"
    assert raw["access_enc"] != b"access-token"
    assert raw["refresh_enc"] != b"refresh-token"

    loaded = await store.get("google")
    assert loaded is not None
    assert loaded.access_token == "access-token"
    assert loaded.refresh_token == "refresh-token"
    assert loaded.account_label == "me@example.com"
    assert loaded.scopes == "read write"

    await store.update_tokens(
        "google",
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        refresh_expires_at=None,
        scopes="read write",
    )
    updated = await store.get("google")
    assert updated is not None
    assert updated.access_token == "new-access"
    assert updated.refresh_token == "new-refresh"

    await store.set_status("google", "reauth_required", "expired")
    assert (await store.get("google")).status == "reauth_required"  # type: ignore[union-attr]

    await store.set_sync("google", "gmail", "history_id", "100")
    assert await store.get_sync("google", "gmail", "history_id") == "100"

    await store.create_state(
        state_hash="state-1",
        provider="google",
        app="gmail",
        scopes="read",
        code_verifier="verifier",
        redirect_uri="https://aether.test/api/oauth/google/callback",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    state = await store.consume_state("state-1")
    assert state is not None
    assert state.code_verifier == "verifier"
    assert state.app == "gmail"
    assert await store.consume_state("state-1") is None

    await store.delete("google")
    assert await store.get("google") is None
    assert await store.get_sync("google", "gmail", "history_id") is None


@pytest.mark.integration
async def test_provider_key_rotation_reencrypts_connections_and_states(db) -> None:
    old_key = generate_key_b64()
    old = TokenKeyring(active_id="v1", active_key_b64=old_key)
    store = ProviderStore(db, old)
    await store.upsert(
        ProviderConnection(
            provider="google",
            auth_mode="oauth",
            access_token="access-token",
            refresh_token="refresh-token",
            scopes="read",
            status="active",
        )
    )
    await store.create_state(
        state_hash="state-2",
        provider="google",
        app="gmail",
        scopes="read",
        code_verifier="verifier",
        redirect_uri="https://aether.test/api/oauth/google/callback",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    new = TokenKeyring(
        active_id="v2",
        active_key_b64=generate_key_b64(),
        previous_id="v1",
        previous_key_b64=old_key,
    )
    assert await _rotate_connections(db, new) == 1
    assert await _rotate_states(db, new) == 1

    row = await db.fetchrow("SELECT key_id FROM provider_connections WHERE provider = 'google'")
    assert row["key_id"] == "v2"
    state_row = await db.fetchrow(
        "SELECT key_id FROM provider_oauth_states WHERE state_hash = 'state-2'"
    )
    assert state_row["key_id"] == "v2"
    loaded = await ProviderStore(db, new).get("google")
    assert loaded is not None
    assert loaded.access_token == "access-token"
    assert loaded.refresh_token == "refresh-token"
    assert await _rotate_connections(db, new) == 0
