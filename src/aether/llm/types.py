"""Unified, provider-agnostic message and tool model.

Every adapter converts between this internal shape and its SDK's native
format, so the agent loop, tool namespace, and persistence never see a
provider-specific structure. Only the adapter modules know what Claude,
OpenAI, Gemini, or Ollama actually speak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


@dataclass
class TextBlock:
    text: str


@dataclass
class ImageBlock:
    """A base64-encoded image for vision turns."""

    data_b64: str
    media_type: str  # "image/png" | "image/jpeg" | ...


@dataclass
class ToolCall:
    """A tool invocation proposed by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """The outcome of executing a ToolCall, fed back to the model.

    `name` is carried alongside `tool_call_id` because Anthropic and OpenAI
    pair results by id while Gemini pairs them by function name.
    """

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


Block = TextBlock | ImageBlock | ToolCall | ToolResult


@dataclass
class Message:
    role: Role
    blocks: list[Block] = field(default_factory=list)
    # Provider-internal round-trip payload (e.g. Anthropic thinking blocks
    # that must be replayed verbatim on the next request). Produced by a
    # provider's response parser, consumed only by that same provider's
    # request mapper. Everyone else leaves it alone.
    provider_extra: Any = None

    @property
    def text(self) -> str:
        """Concatenated text content (tool calls and results excluded)."""
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.blocks if isinstance(b, ToolCall)]

    # -- constructors for the common shapes ---------------------------------

    @classmethod
    def user(
        cls, text: str, images: list[ImageBlock] | None = None
    ) -> "Message":
        blocks: list[Block] = [TextBlock(text)]
        if images:
            blocks.extend(images)
        return cls("user", blocks)

    @classmethod
    def assistant(
        cls,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        provider_extra: Any = None,
    ) -> "Message":
        blocks: list[Block] = []
        if text:
            blocks.append(TextBlock(text))
        if tool_calls:
            blocks.extend(tool_calls)
        return cls("assistant", blocks, provider_extra)

    @classmethod
    def tool_results(cls, results: list[ToolResult]) -> "Message":
        return cls("tool", list(results))


@dataclass
class ToolSpec:
    """A tool exposed to the model: name, description, JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    # Where the tool came from: "native" or an MCP server name. Informational
    # only — authz classifies on the tool name.
    source: str = "native"


@dataclass
class Turn:
    """One provider completion: assistant output before tool execution."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_output_tokens | error
    provider_extra: Any = None
