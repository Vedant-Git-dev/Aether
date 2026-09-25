"""Connector contracts shared by every surface Aether talks through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class UnknownToolError(LookupError):
    """A tool name that isn't in the flat namespace."""


class ConnectorUnavailableError(RuntimeError):
    """A connector (typically an MCP server) is down or never connected."""


@dataclass(frozen=True)
class InboundMessage:
    """One message from the user on some surface.

    `handle` is the sender's handle as the platform wrote it — normalization
    and the contact allowlist happen at ingest, not here.
    """

    surface: str  # "telegram" | "discord" | "slack" | "web"
    handle: str
    text: str
    chat_ref: str  # opaque, surface-specific reply target


InboundHandler = Callable[[InboundMessage], Awaitable[None]]
# (approval_id, APPROVED | DENIED) — the Phase-6 executor's cue
DecisionCallback = Callable[[int, str], Awaitable[None]]


class ApprovalDecider(Protocol):
    """What messaging connectors need from the approvals store."""

    async def decide(self, approval_id: int, decision: str) -> Any: ...


class MessagingConnector:
    """Shared approve/deny + inbound plumbing; SDK glue lives in subclasses.

    Every surface presents the same buttons: a pending approval goes out
    with Approve/Deny attached, the press lands in one place (`_decide`),
    and only a *fresh* decision (not an expired or double-pressed one)
    fires `on_decision`.
    """

    name = ""

    def __init__(
        self,
        *,
        enabled: bool,
        approvals: ApprovalDecider | None = None,
        on_decision: DecisionCallback | None = None,
        on_inbound: InboundHandler | None = None,
    ) -> None:
        self._enabled = enabled
        self._approvals = approvals
        self._on_decision = on_decision
        self._on_inbound = on_inbound

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _decide(self, approval_id: int, decision: str) -> Any:
        """The one shared path behind every platform's approve/deny buttons."""
        result = None
        if self._approvals is not None:
            result = await self._approvals.decide(approval_id, decision)
        if result is not None and self._on_decision is not None:
            await self._on_decision(approval_id, decision)
        return result

    async def _emit_inbound(self, message: InboundMessage) -> None:
        if self._on_inbound is not None:
            await self._on_inbound(message)
