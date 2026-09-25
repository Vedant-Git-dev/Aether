"""Gemini adapter — official `google-genai` SDK, async client."""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types as gtypes

from .base import ProviderError
from .types import ImageBlock, Message, TextBlock, ToolCall, ToolResult, ToolSpec, Turn

# JSON Schema spellings -> the casing the genai SDK's Schema type expects.
_TYPE_NAMES = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "any": "ANY",
}


def _schema_to_gemini(schema: Any) -> Any:
    """Recursively uppercase `type` names; pass everything else through."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out[key] = _TYPE_NAMES.get(value.lower(), value.upper())
            elif isinstance(value, dict):
                out[key] = _schema_to_gemini(value)
            elif isinstance(value, list):
                out[key] = [_schema_to_gemini(v) for v in value]
            else:
                out[key] = value
        return out
    return schema


def messages_to_gemini(messages: list[Message]) -> list[dict[str, Any]]:
    """Internal messages -> Gemini `contents` bodies."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            parts: list[dict[str, Any]] = []
            for b in msg.blocks:
                if isinstance(b, TextBlock) and b.text:
                    parts.append({"text": b.text})
                elif isinstance(b, ImageBlock):
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": b.media_type,
                                "data": b.data_b64,
                            }
                        }
                    )
            if parts:
                out.append({"role": "user", "parts": parts})
        elif msg.role == "assistant":
            parts = []
            for b in msg.blocks:
                if isinstance(b, TextBlock) and b.text:
                    parts.append({"text": b.text})
                elif isinstance(b, ToolCall):
                    parts.append(
                        {"function_call": {"name": b.name, "args": b.arguments}}
                    )
            if parts:
                out.append({"role": "model", "parts": parts})
        elif msg.role == "tool":
            # Gemini pairs function responses by name, not id, and carries
            # them in a user turn.
            parts = []
            for b in msg.blocks:
                if isinstance(b, ToolResult):
                    parts.append(
                        {
                            "function_response": {
                                "name": b.name or "tool",
                                "response": {"result": b.content},
                            }
                        }
                    )
            if parts:
                out.append({"role": "user", "parts": parts})
    return out


def tools_to_gemini(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "function_declarations": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": _schema_to_gemini(t.input_schema),
                }
                for t in tools
            ]
        }
    ]


def turn_from_gemini(response: Any) -> Turn:
    """SDK response -> internal Turn. Gemini function calls carry no id, so
    one is synthesized per position; results pair by name anyway."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise ProviderError("gemini: response contained no candidates")
    candidate = candidates[0]
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for i, part in enumerate(parts):
        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            calls.append(
                ToolCall(
                    id=f"gemini_{i}",
                    name=getattr(function_call, "name", ""),
                    arguments=dict(getattr(function_call, "args", None) or {}),
                )
            )
            continue
        text = getattr(part, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
    finish = str(getattr(candidate, "finish_reason", "") or "")
    if finish.endswith("MAX_TOKENS"):
        reason = "max_output_tokens"
    else:
        reason = "tool_use" if calls else "end_turn"
    return Turn(text="".join(text_parts), tool_calls=calls, stop_reason=reason)


class GeminiProvider:
    name = "gemini"
    supports_tools = True

    def __init__(
        self,
        client: genai.Client,
        model: str,
        max_tokens: int = 16000,
        supports_vision: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.supports_vision = supports_vision

    @classmethod
    def build(cls, api_key: str, model: str, max_tokens: int = 16000) -> "GeminiProvider":
        if not api_key:
            raise ProviderError(
                "llm.provider is gemini but GEMINI_API_KEY is not set — "
                "add it to .env, or point llm.provider at a different provider in config.yaml"
            )
        return cls(genai.Client(api_key=api_key), model, max_tokens)

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Turn:
        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": self.max_tokens,
        }
        if tools:
            config_kwargs["tools"] = tools_to_gemini(tools)
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=messages_to_gemini(messages),
                config=gtypes.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:
            raise ProviderError(f"gemini call failed: {exc}") from exc
        return turn_from_gemini(response)
