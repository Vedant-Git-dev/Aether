"""Groq adapter — Groq's OpenAI-compatible endpoint.

Same wire format as OpenAI, so this reuses that adapter wholesale and only
swaps the endpoint and the vision capability — Groq hosts both text-only
and vision-capable models under one API, so whether a given model id
accepts images is a config choice, the same way it is for Ollama and
NVIDIA NIM.

It deliberately sends no token cap. Groq decides admission on the prompt
plus the requested maximum output, so a large `max_tokens` can be refused
against a model's tokens-per-minute budget before a single word is
generated — a refusal backoff cannot clear. Omitting the parameter lets
the model's own ceiling apply, which is both the repo's never-cap rule and
the honest fit for Groq's per-minute accounting.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from .base import ProviderError
from .openai_provider import OpenAIProvider

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(OpenAIProvider):
    name = "groq"

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = False,
    ) -> None:
        # max_tokens_param=None: Groq gets no token cap on the wire. The
        # value is still carried for callers that read it, but it is never
        # sent.
        super().__init__(
            client,
            model,
            max_tokens,
            supports_vision,
            max_tokens_param=None,
        )

    @classmethod
    def build(
        cls,
        api_key: str,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = False,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> GroqProvider:
        if not api_key:
            raise ProviderError(
                "llm.provider is groq but GROQ_API_KEY is not set — "
                "add it to .env, or point llm.provider at a different provider in config.yaml"
            )
        return cls(
            AsyncOpenAI(base_url=base_url or _DEFAULT_BASE_URL, api_key=api_key),
            model,
            max_tokens,
            supports_vision,
        )
