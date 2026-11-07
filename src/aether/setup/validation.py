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
import importlib.util
import re

from ..config_validation import is_missing, normalize_database_url

_NETWORK = True
_TIMEOUT = 10.0
_CREDENTIALS = re.compile(r"(postgres(?:ql)?://)[^@\s]+@")

_RENDER_API = "https://api.render.com/v1"
# A Render web service's name becomes its subdomain, so it must be
# subdomain-safe: lowercase letters, numbers and hyphens.
_RENDER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,28}[a-z0-9]$")


def use_network(enabled: bool) -> None:
    """Turn the live probes on or off. Off is the hermetic test mode."""
    global _NETWORK
    _NETWORK = enabled


def network_enabled() -> bool:
    return _NETWORK


def _run(coro):
    """Run one probe from the synchronous wizard."""
    return asyncio.run(coro)


def _installed(*modules: str) -> bool:
    """Whether the probe's own libraries are importable here.

    The wizard can run from its bootstrap virtual environment, which holds
    only rich and prompt_toolkit. A missing client library means the value
    cannot be checked, not that the value is wrong, so the caller lets it
    through instead of refusing a correct key.
    """
    try:
        return all(importlib.util.find_spec(name) is not None for name in modules)
    except Exception:
        return False


def _reason(exc: Exception, url: str) -> str:
    """One short line from a failed probe, without echoing the URL."""
    text = str(exc).splitlines()[0].strip()
    if url and url in text:
        text = text.replace(url, "(connection string)")
    text = _CREDENTIALS.sub(r"\1***@", text)
    return (text or type(exc).__name__)[:160]


def database_error(url: str) -> str | None:
    """A Postgres connection string that is shaped right and connects."""
    value = (url or "").strip()
    if is_missing(value):
        return "DATABASE_URL is required — paste the connection string."
    if not value.startswith(("postgresql://", "postgres://")):
        return "a Postgres URL starts with postgresql:// or postgres://."
    if not _NETWORK or not _installed("asyncpg"):
        return None
    try:
        _run(_probe_database(value))
    except Exception as exc:
        return (
            f"could not connect — {_reason(exc, value)}. Check the host, user, "
            "password and sslmode=require, then try again."
        )
    return None


async def _probe_database(url: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(normalize_database_url(url), timeout=_TIMEOUT)
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
        if not _NETWORK or not _installed("openai"):
            return None
        try:
            _run(_probe_models(provider, value))
        except Exception:
            return "could not reach the local Ollama host — start it and try again."
        return None
    if is_missing(value):
        return "this key is required for the chosen provider."
    if not _NETWORK or not _installed("anthropic", "openai", "google.genai"):
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
    if not _NETWORK or not _installed("google.genai"):
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


def _render_owner_id(key: str) -> str:
    """The first workspace id the key can see, or "" when it cannot be read."""
    import httpx

    try:
        response = httpx.get(
            f"{_RENDER_API}/owners",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except Exception:
        return ""
    if response.status_code != 200:
        return ""
    try:
        data = response.json()
    except Exception:
        return ""
    if isinstance(data, list) and data:
        first = data[0]
        owner = first.get("owner", first) if isinstance(first, dict) else {}
        return str(owner.get("id", "") or "")
    return ""


def render_api_key_error(key: str) -> str | None:
    """Render accepts the API key and shows at least one workspace."""
    value = (key or "").strip()
    if is_missing(value):
        return "a Render API key is required to deploy."
    if not _NETWORK or not _installed("httpx"):
        return None
    import httpx

    try:
        response = httpx.get(
            f"{_RENDER_API}/owners",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {value}", "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except Exception:
        return None
    if response.status_code in (401, 403):
        return "Render rejected the API key — copy it again from the dashboard."
    return None


def render_service_name_error(name: str, key: str) -> str | None:
    """The service name is free to use in the key's Render workspace."""
    value = (name or "").strip()
    if is_missing(value):
        return "a service name is required."
    if not _RENDER_NAME.match(value):
        return "use 2-30 characters: lowercase letters, numbers and hyphens."
    if is_missing(key) or not _NETWORK or not _installed("httpx"):
        return None
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    owner_id = _render_owner_id(key)
    params: dict[str, object] = {"name": value, "limit": 20}
    if owner_id:
        params["ownerId"] = owner_id
    try:
        response = httpx.get(
            f"{_RENDER_API}/services", params=params, headers=headers, timeout=_TIMEOUT
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        items = response.json()
    except Exception:
        return None
    for item in items if isinstance(items, list) else []:
        service = item.get("service", item) if isinstance(item, dict) else {}
        if isinstance(service, dict) and service.get("name") == value:
            return f"'{value}' is already taken on Render — choose another name."
    return None


def telegram_token_error(token: str) -> str | None:
    """Telegram accepts the bot token (getMe)."""
    value = (token or "").strip()
    if is_missing(value):
        return "a Telegram bot token is required when Telegram is on."
    if not _NETWORK or not _installed("httpx"):
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
    if not _NETWORK or not _installed("httpx"):
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
