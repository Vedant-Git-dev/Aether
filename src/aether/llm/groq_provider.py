"""Groq adapter — Groq's OpenAI-compatible endpoint.

Same wire format as OpenAI, so this reuses that adapter wholesale and only
swaps the endpoint, the max-token spelling (Groq takes the older
`max_tokens`, not `max_completion_tokens`), and the vision capability —
Groq hosts both text-only and vision-capable models under one API, so
whether a given model id accepts images is a config choice, the same way
it is for Ollama and NVIDIA NIM.
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
        super().__init__(
            client,
            model,
            max_tokens,
            supports_vision,
            max_tokens_param="max_tokens",
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
