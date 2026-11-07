"""Live checks for the wizard's required answers.

Each check returns ``None`` when the value is good, or one plain sentence
with the fix. Presence and placeholder rules live in
``aether.config_validation``; this module adds the real probes: a database
connection, a provider's model-list call, a Gemini embedding, and the two
bot identity endpoints.

The probes are network calls, so the unit suite turns them off through
``use_network(False)`` (the conftest fixture does this). The production
wizard leaves them on.
"""

from __future__ import annotations

import asyncio

from ..config_validation import is_missing

_NETWORK = True
_TIMEOUT = 10.0


def use_network(enabled: bool) -> None:
    """Turn the live probes on or off. Off is the hermetic test mode."""
    global _NETWORK
    _NETWORK = enabled


def network_enabled() -> bool:
    return _NETWORK


def _run(coro):
    """Run one probe from the synchronous wizard."""
    return asyncio.run(coro)


def database_error(url: str) -> str | None:
    """A Postgres connection string that is shaped right and connects."""
    value = (url or "").strip()
    if is_missing(value):
        return "DATABASE_URL is required — paste the connection string."
    if not value.startswith(("postgresql://", "postgres://")):
        return "a Postgres URL starts with postgresql:// or postgres://."
    if not _NETWORK:
        return None
    try:
        _run(_probe_database(value))
    except Exception:
        return (
            "could not connect — check the host, user, password and "
            "sslmode=require, then try again."
        )
    return None


async def _probe_database(url: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(url, timeout=_TIMEOUT)
    try:
        await connection.fetchval("SELECT 1")
    finally:
        await connection.close()


def model_key_error(provider: str, key: str, base_url: str | None = None) -> str | None:
    """The chosen provider answers a model-list call with this key."""
    value = (key or "").strip()
    if provider == "ollama":
        if is_missing(value):
            return "OLLAMA_BASE_URL is required — the local host address."
        if not _NETWORK:
            return None
        try:
            _run(_probe_models(provider, value))
        except Exception:
            return "could not reach the local Ollama host — start it and try again."
        return None
    if is_missing(value):
        return "this key is required for the chosen provider."
    if not _NETWORK:
        return None
    try:
        _run(_probe_models(provider, value, base_url))
    except Exception:
        return "the key could not be verified — check it and try again."
    return None


async def _probe_models(provider: str, key: str, base_url: str | None = None) -> None:
    from ..llm.model_list import fetch_models

    await asyncio.wait_for(fetch_models(provider, key, base_url), timeout=_TIMEOUT)


def embedding_key_error(key: str, model: str) -> str | None:
    """The Gemini key makes one small embedding with the chosen model."""
    value = (key or "").strip()
    if is_missing(value):
        return "a Gemini API key is required when memory search is on."
    if not _NETWORK:
        return None
    try:
        _run(_probe_embedding(value, model))
    except Exception:
        return "the Gemini key could not make an embedding — check it and try again."
    return None


async def _probe_embedding(key: str, model: str) -> None:
    from ..llm.embeddings import GeminiEmbedder

    embedder = GeminiEmbedder.build(key, model)
    await asyncio.wait_for(embedder.embed(["setup check"], "query"), timeout=_TIMEOUT)


def telegram_token_error(token: str) -> str | None:
    """Telegram accepts the bot token (getMe)."""
    value = (token or "").strip()
    if is_missing(value):
        return "a Telegram bot token is required when Telegram is on."
    if not _NETWORK:
        return None
    import httpx

    try:
        response = httpx.get(f"https://api.telegram.org/bot{value}/getMe", timeout=_TIMEOUT)
    except Exception:
        return "Telegram did not answer — check your network and the token."
    if response.status_code == 200 and response.json().get("ok") is True:
        return None
    if response.status_code in (401, 404):
        return "Telegram rejected the bot token — copy it again from BotFather."
    return "Telegram did not accept the token — check it and try again."


def discord_token_error(token: str) -> str | None:
    """Discord accepts the bot token (users/@me)."""
    value = (token or "").strip()
    if is_missing(value):
        return "a Discord bot token is required when Discord is on."
    if not _NETWORK:
        return None
    import httpx

    try:
        response = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {value}"},
            timeout=_TIMEOUT,
        )
    except Exception:
        return "Discord did not answer — check your network and the token."
    if response.status_code == 200:
        return None
    if response.status_code in (401, 403):
        return "Discord rejected the bot token — reset it and copy it again."
    return "Discord did not accept the token — check it and try again."
