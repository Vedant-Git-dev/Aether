"""Ollama adapter — the local server's OpenAI-compatible endpoint.

Ollama speaks the chat-completions wire format, so this reuses the OpenAI
adapter wholesale and only swaps the endpoint, the max-token spelling, and
the vision capability (which depends entirely on which model the user
pulled).
"""

from __future__ import annotations

from openai import AsyncOpenAI

from .base import ProviderError
from .openai_provider import OpenAIProvider


class OllamaProvider(OpenAIProvider):
    name = "ollama"

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
        base_url: str,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = False,
    ) -> "OllamaProvider":
        if not base_url:
            raise ProviderError(
                "llm.provider is ollama but OLLAMA_BASE_URL is not set"
            )
        # Ollama ignores the api key; the OpenAI SDK requires one to exist.
        return cls(AsyncOpenAI(base_url=base_url, api_key="ollama"), model, max_tokens, supports_vision)
