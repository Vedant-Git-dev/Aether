"""Grants and health: what the user has allowed, and what is up.

Visibility is derived, never stored per tool. A capability is granted when
the app that serves it is connected (Gmail, GitHub, a configured MCP
server) or when it is one of Aether's own wired capabilities. A backend is
healthy when the connector watchdog says so; an unresponsive server's tools
leave the prompt and its calls are refused instead of hanging.
"""

from __future__ import annotations

from collections.abc import Callable

# Aether's own capabilities are granted when the store behind them is wired,
# which is already how registration works.
INTERNAL_CAPABILITIES = frozenset(
    {
        "tools",
        "memory",
        "message",
        "approvals",
        "schedule",
        "screen",
        "media",
        "routines",
        "watches",
        "traces",
        "audit",
        "config",
        "workspace",
        "web",
    }
)


class CapabilityGrants:
    """Whether one capability may be shown and called this turn."""

    def __init__(self, is_granted: Callable[[str], bool] | None = None) -> None:
        self._is_granted = is_granted or (lambda capability: True)

    def granted(self, capability: str) -> bool:
        if capability in INTERNAL_CAPABILITIES:
            return True
        try:
            return bool(self._is_granted(capability))
        except Exception:
            return False


class BackendHealth:
    """Whether one backend (provider app, MCP server) is responding."""

    def __init__(self, is_healthy: Callable[[str], bool] | None = None) -> None:
        self._is_healthy = is_healthy or (lambda backend: True)

    def healthy(self, backend: str) -> bool:
        if not backend:
            return True
        try:
            return bool(self._is_healthy(backend))
        except Exception:
            return False


ALL_GRANTED = CapabilityGrants()
ALL_HEALTHY = BackendHealth()
