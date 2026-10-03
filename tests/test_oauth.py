"""OAuth from chat — the state that makes a callback self-verifying, the
code exchange, and the encrypted home for what the provider hands back.

No network: the token endpoint is a recording fake, and the store's pool
is a dict behind asyncpg's surface — but the encryption in every roundtrip
is the real Cipher, because that's the property the design rests on.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.oauth import (
    PROVIDERS,
    OAuthError,
    OAuthFlow,
    OAuthRefresher,
    OAuthTokenStore,
    TokenSet,
    exchange_code,
    refresh_access,
)

KEY = generate_key_b64()  # a fresh 32-byte key, the way boot makes one


def _tokens(access: str = "ya29.old", expires_in: float = 60.0) -> TokenSet:
    return TokenSet(
        access_token=access,
        refresh_token="1//keep",
        scopes="https://www.googleapis.com/auth/gmail.modify",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


# -- consent links and the state that rides them -------------------------------


def test_the_consent_link_carries_offline_consent_and_a_verifiable_state() -> None:
    flow = OAuthFlow(KEY)
    url = flow.consent_url(
        provider="google",
        app="gmail",
        client_id="cid-123",
        redirect="https://example.aether.app/oauth/callback",
        scopes="https://www.googleapis.com/auth/gmail.modify",
    )
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https", "accounts.google.com", "/o/oauth2/v2/auth",
    )
    params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == "cid-123"
    assert params["redirect_uri"] == "https://example.aether.app/oauth/callback"
    assert params["scope"] == "https://www.googleapis.com/auth/gmail.modify"
    assert params["access_type"] == "offline"  # a refresh token — sign-in is once
    assert params["prompt"] == "consent"
    # the state says which app and which redirect, and can't be forged
    assert flow.verify_state(params["state"]) == (
        "google", "gmail", "https://example.aether.app/oauth/callback",
    )


def test_a_state_is_good_for_ten_minutes_and_no_longer() -> None:
    flow = OAuthFlow(KEY)
    url = flow.consent_url(
        provider="google", app="gcal", client_id="cid",
        redirect="https://r.example/cb", scopes="s", now=1000.0,
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    assert flow.verify_state(state, now=1599.0) == ("google", "gcal", "https://r.example/cb")
    assert flow.verify_state(state, now=1601.0) is None  # past the ten minutes


def test_a_tampered_state_is_rejected() -> None:
    """The whole proof of the public callback route: a forged payload under
    the original signature — gmail posing as gcal — verifies as nothing."""
    flow = OAuthFlow(KEY)
    state = flow._sign("google", "gmail", "https://r.example/cb", 9999999999.0)
    assert flow.verify_state(state) is not None  # the genuine one verifies

    text = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
    payload, _, sig = text.rpartition("|")
    payload = payload.replace("gmail", "gcal")  # the forged app, same signature
    tampered = base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode("ascii")
    assert flow.verify_state(tampered) is None


def test_a_state_from_another_deployment_or_a_garbage_one_is_rejected() -> None:
    state = OAuthFlow(KEY)._sign("google", "gmail", "https://r.example/cb", 9999999999.0)
    assert OAuthFlow(generate_key_b64()).verify_state(state) is None  # different key
    assert OAuthFlow(KEY).verify_state("nonsense") is None
    assert OAuthFlow(KEY).verify_state("") is None


def test_the_flow_refuses_a_missing_or_malformed_key() -> None:
    """Consent links are refused in plain words rather than crashing the
    walk when the permanent encryption key isn't there."""
    with pytest.raises(OAuthError):
        OAuthFlow("")
    with pytest.raises(OAuthError):
        OAuthFlow("definitely not base64!!")
    with pytest.raises(OAuthError):
        OAuthFlow(base64.b64encode(b"too-short").decode("ascii"))


# -- the token endpoint, through a recording fake ------------------------------


class _FakePost:
    """The provider's token endpoint as a test double — records the POST,
    answers with a fixed body."""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, data: dict) -> dict:
        self.calls.append((url, dict(data)))
        return dict(self.body)


async def test_exchange_trades_the_code_for_tokens() -> None:
    post = _FakePost(
        {"access_token": "ya29.a", "expires_in": 3600,
         "refresh_token": "1//fresh", "scope": "gmail.modify"}
    )
    tokens = await exchange_code(
        "google", code="4/0Abc", client_id="cid", client_secret="cs",
        redirect="https://r.example/cb", post=post, now=1000.0,
    )
    url, data = post.calls[0]
    assert url == PROVIDERS["google"].token_url
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "4/0Abc"
    assert data["redirect_uri"] == "https://r.example/cb"  # the state's, replayed
    assert data["client_id"] == "cid" and data["client_secret"] == "cs"
    assert tokens.access_token == "ya29.a"
    assert tokens.refresh_token == "1//fresh"
    assert tokens.scopes == "gmail.modify"
    assert tokens.expires_at == datetime.fromtimestamp(4600.0, UTC)


async def test_refresh_keeps_the_stored_token_when_none_is_reissued() -> None:
    post = _FakePost({"access_token": "ya29.new", "expires_in": 3600})  # no refresh_token
    tokens = await refresh_access(
        "google", refresh_token="1//keep", client_id="cid", client_secret="cs",
        post=post, now=0.0,
    )
    assert tokens.access_token == "ya29.new"
    assert tokens.refresh_token == "1//keep"  # never lost to a terse provider
    assert post.calls[0][1]["grant_type"] == "refresh_token"
    assert post.calls[0][1]["refresh_token"] == "1//keep"


async def test_a_refused_exchange_raises_in_plain_words() -> None:
    post = _FakePost({"error": "invalid_grant", "error_description": "code already used"})
    with pytest.raises(OAuthError) as exc:
        await exchange_code(
            "google", code="x", client_id="cid", client_secret="cs",
            redirect="https://r.example/cb", post=post,
        )
    assert "invalid_grant" in str(exc.value)
    assert "code already used" in str(exc.value)


# -- the encrypted store: real cipher, a pool that's just a dict ----------------


class FakePool:
    """asyncpg's surface over an in-memory table — the roundtrip through it
    is real AES-256-GCM with path-bound AAD."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def execute(self, sql: str, *args) -> None:
        if sql.lstrip().startswith("INSERT"):
            provider, access_enc, refresh_enc, scopes, expires_at = args
            self.rows[provider] = {
                "provider": provider, "access_enc": access_enc,
                "refresh_enc": refresh_enc, "scopes": scopes,
                "expires_at": expires_at,
            }
        elif sql.lstrip().startswith("UPDATE"):
            provider, access_enc, expires_at = args
            self.rows[provider]["access_enc"] = access_enc
            self.rows[provider]["expires_at"] = expires_at

    async def fetchrow(self, sql: str, provider: str) -> dict | None:
        return self.rows.get(provider)

    async def fetch(self, sql: str) -> list[dict]:
        return list(self.rows.values())


def _store() -> tuple[FakePool, OAuthTokenStore]:
    pool = FakePool()
    return pool, OAuthTokenStore(pool, Cipher.from_b64(KEY))


async def test_the_store_roundtrips_tokens_encrypted() -> None:
    pool, store = _store()
    tokens = _tokens(access="ya29.a", expires_in=3600.0)

    await store.save("google", tokens)
    row = pool.rows["google"]
    assert b"ya29.a" not in row["access_enc"]  # the value never sits in the row
    assert b"1//keep" not in row["refresh_enc"]

    assert await store.get("google") == tokens
    assert await store.get("github") is None
    assert await store.all() == {"google": tokens}

    await store.update_access("google", "ya29.b", datetime.now(UTC) + timedelta(hours=2))
    refreshed = await store.get("google")
    assert refreshed is not None
    assert refreshed.access_token == "ya29.b"
    assert refreshed.refresh_token == "1//keep"  # the refresher touches only access


async def test_a_swapped_blob_fails_to_masquerade() -> None:
    """AAD binds each ciphertext to its column — a refresh ciphertext pasted
    into the access column decrypts as nothing."""
    pool, store = _store()
    await store.save("google", _tokens())
    pool.rows["google"]["access_enc"] = pool.rows["google"]["refresh_enc"]
    with pytest.raises(OAuthError):
        await store.get("google")


# -- the background refresher ---------------------------------------------------


class FakeStore:
    """The store's surface as the refresher sees it, recording updates."""

    def __init__(self, tokens: dict[str, TokenSet]) -> None:
        self.tokens = dict(tokens)
        self.updates: list[tuple[str, str]] = []

    async def all(self) -> dict[str, TokenSet]:
        return dict(self.tokens)

    async def update_access(self, provider: str, access_token: str, expires_at) -> None:
        self.updates.append((provider, access_token))


class FakeResolver:
    """resolve(name) -> value — and a record of every name read."""

    def __init__(self, **values: str) -> None:
        self.values = values
        self.reads: list[str] = []

    async def resolve(self, name: str) -> str | None:
        self.reads.append(name)
        return self.values.get(name)


async def test_the_refresher_refreshes_only_tokens_nearing_expiry(monkeypatch) -> None:
    async def fake_refresh(provider, *, refresh_token, client_id, client_secret):
        assert client_id == "cid" and client_secret == "cs"
        return TokenSet(
            access_token="ya29.new", refresh_token=refresh_token, scopes="s",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    monkeypatch.setattr("aether.oauth.refresh_access", fake_refresh)
    store = FakeStore({
        "google": _tokens(expires_in=60),   # inside the five-minute window
        "far": _tokens(access="ya29.far", expires_in=3600.0),
    })
    refresher = OAuthRefresher(
        store, resolver=FakeResolver(GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="cs")
    )
    assert await refresher.tick() == 1
    assert store.updates == [("google", "ya29.new")]  # the far one was left alone


async def test_a_failed_refresh_logs_and_the_pass_continues(monkeypatch, caplog) -> None:
    async def fake_refresh(provider, *, refresh_token, client_id, client_secret):
        if provider == "google":
            raise OAuthError("the provider refused: invalid_grant")
        return TokenSet(
            access_token="ya29.other", refresh_token=refresh_token, scopes="s",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    monkeypatch.setattr("aether.oauth.refresh_access", fake_refresh)
    store = FakeStore({"google": _tokens(), "other": _tokens(access="ya29.o")})
    refresher = OAuthRefresher(
        store, resolver=FakeResolver(GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="cs")
    )
    with caplog.at_level(logging.WARNING, logger="aether.oauth"):
        assert await refresher.tick() == 1
    assert store.updates == [("other", "ya29.other")]  # the pass went on
    assert "google" in caplog.text
    assert "trying again next cycle" in caplog.text


async def test_no_stored_tokens_is_a_pass_that_reads_no_keys(monkeypatch) -> None:
    async def must_not_refresh(*args, **kwargs):
        raise AssertionError("nothing was stored — no refresh should run")

    monkeypatch.setattr("aether.oauth.refresh_access", must_not_refresh)
    resolver = FakeResolver(GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="cs")
    refresher = OAuthRefresher(FakeStore({}), resolver=resolver)
    assert await refresher.tick() == 0
    assert resolver.reads == []  # no sign-ins → the client keys aren't even read


async def test_missing_client_keys_skip_the_pass_entirely(monkeypatch) -> None:
    async def must_not_refresh(*args, **kwargs):
        raise AssertionError("no client credentials — no refresh should run")

    monkeypatch.setattr("aether.oauth.refresh_access", must_not_refresh)
    refresher = OAuthRefresher(FakeStore({"google": _tokens()}), resolver=FakeResolver())
    assert await refresher.tick() == 0


async def test_start_and_stop_end_cleanly() -> None:
    refresher = OAuthRefresher(FakeStore({}), resolver=FakeResolver(), interval=3600.0)
    refresher.start()
    await refresher.stop()
    await refresher.stop()  # idempotent — a second stop is a no-op
