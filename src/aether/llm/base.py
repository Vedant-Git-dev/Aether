"""The Provider protocol every adapter implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import Message, ToolSpec, Turn


class ProviderError(RuntimeError):
    """A provider could not be built or a completion call failed
    (missing credentials, network, rate limit, malformed request)."""


@runtime_checkable
class Provider(Protocol):
    """One LLM backend, normalized to a single completion method.

    `complete` is a single request/response exchange — no tool execution.
    The manual tool-use loop in `agent.py` drives it.

    Which model handles a medium is user config (the tri-state knobs), not
    a provider claim — the chosen model's own acceptance is discovered at
    call time. An adapter that cannot carry a medium's block fails loudly
    in its mapper (user media is never silently dropped); the caller's
    ladder catches that refusal and degrades — video to workbench frames,
    audio to transcription — or says so plainly.
    """

    name: str  # "anthropic" | "openai" | "gemini" | "ollama" | "nvidia" | "fake"
    model: str  # concrete model id sent to the API
    supports_tools: bool
    supports_vision: bool  # accepts ImageBlock

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Turn: ...
