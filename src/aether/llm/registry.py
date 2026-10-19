"""Resolve configuration into Provider instances, one per role.

A single provider backs the agent, with optional per-role model overrides
that build extra instances of the same provider class with a different
model id — one provider choice governs everything, while each job can
run on a smaller/cheaper model where it makes sense.

Perception is per-medium: image, audio, and video are separate roles,
each set by a tri-state knob — off (default), "base" (the main model
takes it), or a specific model id. The knob is the routing truth; what
the chosen model truly accepts is discovered at call time, and the
caller's ladder degrades honestly on a refusal.
"""

from __future__ import annotations

from ..config import LLMConfig, Settings
from .anthropic_provider import AnthropicProvider
from .base import Provider, ProviderError
from .gemini_provider import GeminiProvider
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

ROLES = ("reasoning", "salience", "image", "audio", "video")

# the knob's word for "the main model takes this medium"
BASE = "base"


def _build(settings: Settings, llm: LLMConfig, model: str, vision_capable: bool) -> Provider:
    if llm.provider == "anthropic":
        return AnthropicProvider.build(settings.anthropic_api_key, model, llm.max_tokens)
    if llm.provider == "openai":
        return OpenAIProvider.build(settings.openai_api_key, model, llm.max_tokens)
    if llm.provider == "gemini":
        return GeminiProvider.build(settings.gemini_api_key, model, llm.max_tokens)
    if llm.provider == "ollama":
        return OllamaProvider.build(
            settings.ollama_base_url, model, llm.max_tokens, supports_vision=vision_capable
        )
    raise ProviderError(f"unknown llm.provider: {llm.provider!r}")


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
        # Whether the default instance can see images. For hosted providers
        # that's inherent; for Ollama it depends entirely on the model the
        # user pulled, so it's an explicit config flag.
        default_vision = llm.ollama_vision if llm.provider == "ollama" else True
        default = _build(settings, llm, llm.model, default_vision)

        providers: dict[str, Provider | None] = {
            "reasoning": default,
            "salience": default,
            "image": _medium(settings, llm, default, llm.image_model),
            "audio": _medium(settings, llm, default, llm.audio_model),
            "video": _medium(settings, llm, default, llm.video_model),
        }
        if llm.salience_model and llm.salience_model != llm.model:
            providers["salience"] = _build(settings, llm, llm.salience_model, default_vision)
        return cls(providers)


def _medium(
    settings: Settings,
    llm: LLMConfig,
    base: Provider,
    knob: str | None,
) -> Provider | None:
    """One perception role from its tri-state knob: off -> None (the
    default), "base" -> the main model, or a specific model id (a
    deliberate choice). Whether the chosen model truly accepts the medium
    is discovered at call time — a refusal falls to the caller's ladder,
    which degrades honestly."""
    if not knob:
        return None  # off — the default
    if knob == BASE:
        return base
    return _build(settings, llm, knob, vision_capable=True)
