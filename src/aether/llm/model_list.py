"""The models the configured keys can actually reach — fetched live from
each provider's own models-list endpoint, never hardcoded.

Only a provider with its key (Ollama: its address) configured appears,
and the resolver — the same truth the Settings page's credential badges
read — decides what "configured" means, so a pasted key counts without a
restart and a removed key drops its provider from the list. Each
provider's fetch is isolated: one that doesn't answer leaves the others
listed and carries a plain inline note, never a broken dropdown.

A key's value never leaves this module. It is held in memory only to
detect "the key changed, refetch", and it names nothing in the feed, the
errors, or the log. Results are cached per provider keyed by that value,
so the same key is fetched once until it changes or a refresh is asked
for; a failed fetch is not cached, so a transient blip heals on the next
open without hammering anything.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from anthropic import AsyncAnthropic
from google import genai
from openai import AsyncOpenAI

from ..config import MODEL_BASE, MODEL_DEFAULT, MODEL_OFF

log = logging.getLogger("aether.llm.model_list")

# the secret name each provider is "configured" by — Ollama has no key, its
# address is what makes it reachable
_PROVIDER_SECRETS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}

# the plain words a failed fetch carries — the provider's name rides with
# the group in the feed, so the note itself stays generic
_FETCH_ERROR = "couldn't be reached — its models are left out this time"


def _default_client(provider: str, secret: str) -> Any:
    """The same SDK client the provider's own adapter builds — one list
    call, nothing else, so the dropdown and the chat wire can't disagree
    about what a key reaches."""
    if provider == "anthropic":
        return AsyncAnthropic(api_key=secret)
    if provider == "openai":
        return AsyncOpenAI(api_key=secret)
    if provider == "gemini":
        return genai.Client(api_key=secret)
    if provider == "ollama":
        # Ollama ignores the api key; the OpenAI SDK requires one to exist.
        return AsyncOpenAI(base_url=secret, api_key="ollama")
    raise ValueError(f"unknown provider: {provider}")


async def _fetch_anthropic(client: Any) -> list[str]:
    # the SDK hands back the paginator itself — no await, just the walk
    return sorted([model.id async for model in client.models.list()])


async def _fetch_openai_style(client: Any) -> list[str]:
    page = await client.models.list()
    return sorted(model.id for model in page.data)


async def _fetch_gemini(client: Any) -> list[str]:
    pager = await client.aio.models.list()
    # Gemini names its models "models/<id>" on this endpoint — the id the
    # chat wire wants is what follows the prefix
    return sorted([model.name.removeprefix("models/") async for model in pager])


_FETCHERS = {
    "anthropic": _fetch_anthropic,
    "openai": _fetch_openai_style,
    "ollama": _fetch_openai_style,
    "gemini": _fetch_gemini,
}


def is_sentinel(value: str | None) -> bool:
    """True for the words that name a meaning, never a model — so callers
    echoing configured ids (the compat models route, an availability note)
    can keep sentinels out of what reads like a model list."""
    return value in (None, "", MODEL_OFF, MODEL_BASE, MODEL_DEFAULT)


class ModelCatalog:
    """Per-provider model lists with a by-key cache. One instance lives on
    app.state; the Settings endpoint asks it for the whole feed."""

    def __init__(self, resolver: Any, *, client_for: Any = _default_client) -> None:
        self._resolver = resolver
        self._client_for = client_for
        # provider -> (the key the list was fetched with, the models). The
        # key lives here only to be compared against the next resolve —
        # never logged, never returned.
        self._cache: dict[str, tuple[str, list[str]]] = {}
        self._lock = asyncio.Lock()

    async def feed(self, *, refresh: bool = False) -> dict:
        """The grouped list the dropdown renders: every configured
        provider's models, each with an inline note when its fetch
        failed, and a flag for "no key anywhere" so the page can point at
        the credential settings instead of an empty dropdown."""
        async with self._lock:
            providers: list[dict] = []
            configured = False
            for provider, secret_name in _PROVIDER_SECRETS.items():
                secret = await self._resolver.resolve(secret_name)
                if not secret:
                    # the key is gone — its models go with it, cached or not
                    self._cache.pop(provider, None)
                    continue
                configured = True
                cached = self._cache.get(provider)
                models: list[str]
                if cached is not None and cached[0] == secret and not refresh:
                    models = cached[1]
                else:
                    try:
                        client = self._client_for(provider, secret)
                        models = await _FETCHERS[provider](client)
                    except Exception:
                        # never the key in the log — the traceback of these
                        # SDKs carries the request, not the auth material
                        log.warning(
                            "models list for %s didn't answer — leaving it out this time",
                            provider,
                            exc_info=True,
                        )
                        providers.append(
                            {"provider": provider, "models": [], "error": _FETCH_ERROR}
                        )
                        continue
                    self._cache[provider] = (secret, models)
                providers.append({"provider": provider, "models": models, "error": ""})
            return {"configured": configured, "providers": providers}
