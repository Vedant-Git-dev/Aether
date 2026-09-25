"""OpenAI adapter — official `openai` SDK (chat completions).

Also the base for the Ollama adapter, which speaks this wire format.
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .base import ProviderError
from .types import ImageBlock, Message, TextBlock, ToolCall, ToolResult, ToolSpec, Turn


def messages_to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Internal messages -> OpenAI chat-completions bodies (system included)."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        if msg.role == "user":
            parts: list[dict[str, Any]] = []
            for b in msg.blocks:
                if isinstance(b, TextBlock) and b.text:
                    parts.append({"type": "text", "text": b.text})
                elif isinstance(b, ImageBlock):
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{b.media_type};base64,{b.data_b64}"
                            },
                        }
                    )
            if parts:
                # Plain text-only messages keep the simple string form —
                # some OpenAI-compatible backends only accept that shape.
                if len(parts) == 1 and parts[0].get("type") == "text":
                    out.append({"role": "user", "content": parts[0]["text"]})
                else:
                    out.append({"role": "user", "content": parts})
        elif msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.text or None}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            out.append(entry)
        elif msg.role == "tool":
            for b in msg.blocks:
                if isinstance(b, ToolResult):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b.tool_call_id,
                            "content": b.content,
                        }
                    )
    return out


def tools_to_openai(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def turn_from_openai(response: Any) -> Turn:
    """SDK response -> internal Turn. Tolerates malformed tool arguments:
    a model that emits invalid JSON still yields a call with empty args for
    the tool layer to report as an error."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ProviderError("openai: response contained no choices")
    choice = choices[0]
    message = choice.message
    calls: list[ToolCall] = []
    for tc in getattr(message, "tool_calls", None) or []:
        arguments: dict[str, Any] = {}
        try:
            parsed = json.loads(getattr(tc.function, "arguments", None) or "{}")
            if isinstance(parsed, dict):
                arguments = parsed
        except json.JSONDecodeError:
            pass
        calls.append(
            ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
        )
    finish = getattr(choice, "finish_reason", None)
    reason = {"tool_calls": "tool_use", "length": "max_output_tokens"}.get(
        finish, "end_turn"
    )
    return Turn(
        text=getattr(message, "content", None) or "",
        tool_calls=calls,
        stop_reason=reason,
    )


class OpenAIProvider:
    name = "openai"
    supports_tools = True

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = True,
        # Current OpenAI models take max_completion_tokens; Ollama's
        # compatible endpoint expects the older max_tokens spelling.
        max_tokens_param: str = "max_completion_tokens",
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.supports_vision = supports_vision
        self.max_tokens_param = max_tokens_param

    @classmethod
    def build(
        cls, api_key: str, model: str, max_tokens: int = 16000
    ) -> "OpenAIProvider":
        if not api_key:
            raise ProviderError(
                "llm.provider is openai but OPENAI_API_KEY is not set — "
                "add it to .env, or point llm.provider at a different provider in config.yaml"
            )
        return cls(AsyncOpenAI(api_key=api_key), model, max_tokens)

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages_to_openai(system, messages),
            self.max_tokens_param: self.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools_to_openai(tools)
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ProviderError(f"{self.name} call failed: {exc}") from exc
        return turn_from_openai(response)
