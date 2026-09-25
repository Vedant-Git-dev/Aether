"""Claude adapter — official `anthropic` SDK against the Messages API."""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic

from .base import ProviderError
from .types import ImageBlock, Message, TextBlock, ToolCall, ToolResult, ToolSpec, Turn

# Adaptive thinking: the model decides per request whether to think and how
# much. Tool results can then reference earlier thinking blocks automatically.
DEFAULT_THINKING: dict[str, Any] | None = {"type": "adaptive"}


def messages_to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    """Internal messages -> Anthropic `messages` request bodies."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            blocks: list[dict[str, Any]] = []
            for b in msg.blocks:
                if isinstance(b, TextBlock) and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ImageBlock):
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": b.media_type,
                                "data": b.data_b64,
                            },
                        }
                    )
            if blocks:
                out.append({"role": "user", "content": blocks})
        elif msg.role == "assistant":
            # provider_extra holds this provider's own thinking blocks —
            # replay them verbatim ahead of the text/tool_use blocks.
            blocks = list(msg.provider_extra or [])
            for b in msg.blocks:
                if isinstance(b, TextBlock) and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolCall):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": b.id,
                            "name": b.name,
                            "input": b.arguments,
                        }
                    )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif msg.role == "tool":
            # Anthropic carries tool results inside a user message.
            blocks = []
            for b in msg.blocks:
                if isinstance(b, ToolResult):
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.tool_call_id,
                            "content": [{"type": "text", "text": b.content}],
                            "is_error": b.is_error,
                        }
                    )
            if blocks:
                out.append({"role": "user", "content": blocks})
    return out


def tools_to_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def turn_from_anthropic(response: Any) -> Turn:
    """SDK response -> internal Turn. Skips anything that isn't text or a
    tool call (thinking blocks are captured separately for round-tripping)."""
    content = getattr(response, "content", None) or []
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    extra: list[Any] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(getattr(block, "text", ""))
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(getattr(block, "input", None) or {}),
                )
            )
        elif kind in ("thinking", "redacted_thinking"):
            extra.append(
                block.model_dump(exclude_none=True)
                if hasattr(block, "model_dump")
                else block
            )
    stop = getattr(response, "stop_reason", None)
    reason = {"tool_use": "tool_use", "max_tokens": "max_output_tokens"}.get(
        stop, "end_turn"
    )
    return Turn(
        text="".join(text_parts),
        tool_calls=calls,
        stop_reason=reason,
        provider_extra=extra or None,
    )


class AnthropicProvider:
    name = "anthropic"
    supports_tools = True

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = True,
        thinking: dict[str, Any] | None = DEFAULT_THINKING,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.supports_vision = supports_vision
        self.thinking = thinking

    @classmethod
    def build(
        cls,
        api_key: str,
        model: str,
        max_tokens: int = 16000,
        thinking: dict[str, Any] | None = DEFAULT_THINKING,
    ) -> "AnthropicProvider":
        if not api_key:
            raise ProviderError(
                "llm.provider is anthropic but ANTHROPIC_API_KEY is not set — "
                "add it to .env, or point llm.provider at a different provider in config.yaml"
            )
        return cls(AsyncAnthropic(api_key=api_key), model, max_tokens, thinking=thinking)

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages_to_anthropic(messages),
        }
        if self.thinking:
            kwargs["thinking"] = self.thinking
        if tools:
            kwargs["tools"] = tools_to_anthropic(tools)
        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as exc:
            raise ProviderError(f"anthropic call failed: {exc}") from exc
        return turn_from_anthropic(response)
