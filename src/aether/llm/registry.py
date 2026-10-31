"""Resolve configuration into Provider instances, one per role.

A single provider backs the agent, with optional per-role model overrides
that build extra instances with a different model id — one provider
choice governs everything, while each job can run on a smaller/cheaper
model where it makes sense.

A model value carries its own provider when it has one: "provider/model",
split on the first slash — the rest of the string is the id that
provider's API answers to, so an id may itself contain slashes
("openai/gpt/gpt-5.6" is OpenAI with the id "gpt/gpt-5.6"). A bare id
routes through the global llm.provider, the legacy spelling; the
sentinel words ("off", "default", "base") name meanings, never models.
NVIDIA NIM ids carry a vendor slash of their own
("meta/llama-3.1-405b-instruct"), so under llm.provider nvidia a prefix
that names no known provider is read as part of the id.

Perception is per-medium: image, audio, and video are separate roles,
each set by a knob — off (default), "default"/"base" (the main model
takes it), or a specific model. The first-pass role has its own off too:
"off" leaves it with no model at all, so the skim genuinely stops. The
knob is the routing truth; what the chosen model truly accepts is
discovered at call time, and the caller's ladder degrades honestly on
a refusal.
"""

from __future__ import annotations

from ..config import (
    MODEL_BASE,
    MODEL_DEFAULT,
    MODEL_OFF,
    LLMConfig,
    Settings,
)
from .anthropic_provider import AnthropicProvider
from .base import Provider, ProviderError
from .gemini_provider import GeminiProvider
from .nvidia_provider import NvidiaProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

ROLES = ("reasoning", "salience", "image", "audio", "video")

# the knob's words for "the main model takes this" — "base" is the legacy
# spelling of "default"; both mean the same thing and both keep working
BASE = MODEL_BASE
DEFAULT = MODEL_DEFAULT
OFF = MODEL_OFF


def _split(model: str) -> tuple[str, str]:
    """(provider, model id) from a stored model value — the first slash is
    the seam. A bare id names no provider of its own and answers through
    the global llm.provider instead, so it resolves here as a plain id."""
    provider, sep, rest = model.partition("/")
    if not sep:
        return "", model
    if not provider or not rest:
        raise ProviderError(
            f"odd model name: {model!r} — expected provider/model, like gemini/gemini-2.5-flash"
        )
    return provider, rest


_PROVIDER_NAMES = ("anthropic", "openai", "gemini", "ollama", "nvidia")


def _route(model: str, fallback: str) -> tuple[str, str]:
    """(provider, model id) for a stored model value.

    A prefix that names a known provider routes there. Under NVIDIA the
    first segment may instead belong to the model id itself — NIM ids
    carry a vendor slash ("meta/llama-3.1-405b-instruct") — so a value
    whose prefix is no known provider is read as the id, whole. Under
    every other provider an unknown prefix stays the typo it looks like.
    """
    provider, model_id = _split(model)
    if not provider:
        return fallback, model_id
    if provider in _PROVIDER_NAMES:
        return provider, model_id
    if fallback == "nvidia":
        return "nvidia", model
    raise ProviderError(
        f"unknown provider in model name: {provider!r} — expected one of "
        "anthropic, openai, gemini, ollama, nvidia"
    )


def resolve_provider(model: str, fallback: str) -> str:
    """The provider a stored model value routes through — its own prefix,
    or the global one for a bare legacy id."""
    provider, _ = _route(model, fallback)
    return provider


def _build(settings: Settings, llm: LLMConfig, model: str, vision_capable: bool) -> Provider:
    provider, model_id = _route(model, llm.provider)
    if provider == "anthropic":
        return AnthropicProvider.build(settings.anthropic_api_key, model_id, llm.max_tokens)
    if provider == "openai":
        return OpenAIProvider.build(settings.openai_api_key, model_id, llm.max_tokens)
    if provider == "gemini":
        return GeminiProvider.build(settings.gemini_api_key, model_id, llm.max_tokens)
    if provider == "ollama":
        return OllamaProvider.build(
            settings.ollama_base_url, model_id, llm.max_tokens, supports_vision=vision_capable
        )
    if provider == "nvidia":
        return NvidiaProvider.build(
            settings.nvidia_api_key,
            model_id,
            llm.max_tokens,
            supports_vision=vision_capable,
            base_url=settings.nvidia_base_url,
        )
    raise ProviderError(
        f"unknown provider in model name: {provider!r} — expected one of "
        "anthropic, openai, gemini, ollama, nvidia"
    )


class ProviderRegistry:
    """The resolved provider for each role; perception roles may be None."""

    def __init__(self, providers: dict[str, Provider | None]) -> None:
        self._providers = providers

    def for_role(self, role: str) -> Provider | None:
        if role in self._providers:
            return self._providers[role]
        return self._providers["reasoning"]  # unknown role -> default provider

    @classmethod
    def from_config(cls, settings: Settings, llm: LLMConfig) -> ProviderRegistry:
        # Whether the default instance can see images. Hosted providers are
        # inherently vision-capable; a local model's vision is declared by
        # the capability knob itself — routing image_model to "base" is the
        # user saying their main model reads images. No per-provider flag:
        # the image field is the one place the choice lives.
        main_provider = resolve_provider(llm.model, llm.provider)
        default_vision = main_provider in ("anthropic", "openai", "gemini") or llm.image_model in (
            BASE,
            DEFAULT,
        )
        default = _build(settings, llm, llm.model, default_vision)

        providers: dict[str, Provider | None] = {
            "reasoning": default,
            "salience": default,
            "image": _medium(settings, llm, default, llm.image_model),
            "audio": _medium(settings, llm, default, llm.audio_model),
            "video": _medium(settings, llm, default, llm.video_model),
        }
        if llm.salience_model == OFF:
            # the honest off: no first pass at all — the event judge reads
            # as not-worth-mentioning by default and the free heuristic
            # gate still runs; person-merging falls back to the main model
            providers["salience"] = None
        elif (
            llm.salience_model
            and llm.salience_model not in (BASE, DEFAULT)
            and llm.salience_model != llm.model
        ):
            providers["salience"] = _build(settings, llm, llm.salience_model, default_vision)
        return cls(providers)


def _medium(
    settings: Settings,
    llm: LLMConfig,
    base: Provider,
    knob: str | None,
) -> Provider | None:
    """One perception role from its knob: "off" or empty -> None (the
    default), "default"/"base" -> the main model, or a specific model (a
    deliberate choice — its own provider when the value carries one).
    Whether the chosen model truly accepts the medium is discovered at
    call time — a refusal falls to the caller's ladder, which degrades
    honestly."""
    if not knob or knob == OFF:
        return None
    if knob in (BASE, DEFAULT):
        return base
    return _build(settings, llm, knob, vision_capable=True)
