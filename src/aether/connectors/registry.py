"""The tool namespace: registration, visibility, indexing and dispatch.

Registration is where duplicates die. A tool is identified by ``id``
(``email.send``) and by its ``(capability, intent)`` pair; both must be
unique, and the error names the tool that already owns the slot. Visibility
is derived per turn from capability grants and backend health, never stored
per tool. The prompt gets a one-line index; full schemas are loaded on
demand and capped.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..llm.types import WireSpec
from ..tools.grants import (
    ALL_GRANTED,
    ALL_HEALTHY,
    INTERNAL_CAPABILITIES,
    BackendHealth,
    CapabilityGrants,
)
from ..tools.model import MAX_SUMMARY, ToolSpec
from .base import UnknownToolError
from .mail_allowlist import MailAllowlist
from .mcp_host import MCPHost

log = logging.getLogger("aether.connectors.registry")

NativeHandler = Callable[[dict[str, Any]], Awaitable[Any]]
Kind = Literal["native", "mcp", "provider"]

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "you",
        "your",
        "me",
        "my",
        "we",
        "our",
        "i",
        "they",
        "them",
        "from",
        "via",
        "with",
        "at",
        "by",
        "as",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "have",
        "has",
        "not",
        "no",
        "if",
        "what",
        "which",
        "how",
        "all",
        "any",
        "some",
    }
)

_RISKY = re.compile(
    r"(^|__|_)(send|reply|forward|post|publish|delete|remove|cancel|pay"
    r"|transfer|invite|book|create)([a-z_]|$)"
)
_READONLY = re.compile(r"(^|__)(list|search|get|read|fetch|find|query)([a-z_]|$)")

# A provider app key becomes the user-facing capability. Gmail is the email
# capability; the backend stays hidden in `backends`.
_PROVIDER_CAPABILITY = {"gmail": "email"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOPWORDS}


def _summarise(description: str, fallback: str) -> str:
    """One line, at most the spec limit, without inventing a sentence."""
    text = " ".join((description or "").split())
    if not text:
        return fallback[:MAX_SUMMARY]
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip()
    return (first or text)[:MAX_SUMMARY].rstrip()


def _risk_for_mcp(name: str) -> Literal["read", "write", "external"]:
    if _RISKY.search(name.lower()):
        return "write"
    if _READONLY.search(name.lower()):
        return "read"
    return "external"


@dataclass
class RegisteredTool:
    spec: ToolSpec
    kind: Kind
    handler: NativeHandler | None = None
    effect: Literal["read", "write", "destructive"] | None = None
    auth_mode: Literal["oauth", "token"] | None = None

    @property
    def wire(self) -> WireSpec:
        return self.spec.wire()


class ToolRegistry:
    """Everything the model can call, one identity per intent."""

    def __init__(
        self,
        *,
        grants: CapabilityGrants | None = None,
        health: BackendHealth | None = None,
        max_loaded: int = 8,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._aliases: dict[str, str] = {}
        self._intents: dict[tuple[str, str], str] = {}
        self._mcp_host: MCPHost | None = None
        self._mail_filter: MailAllowlist | None = None
        self.grants = grants or ALL_GRANTED
        self.health = health or ALL_HEALTHY
        self.max_loaded = max_loaded

    # -- registration ----------------------------------------------------------

    def _claim(self, spec: ToolSpec, *, kind: Kind) -> None:
        """Reserve an id and its (capability, intent) slot.

        A duplicate id is always an error except when the MCP sync is
        refreshing its own tool; a duplicate intent names the tool that
        already owns it. Callers that replace their own set drop it first,
        so a re-registration is deliberate, never a silent shadow.
        """
        existing = self._tools.get(spec.id)
        refresh = kind == "mcp" and existing is not None and existing.kind == "mcp"
        if existing is not None and not refresh:
            raise ValueError(f"tool id {spec.id!r} is already registered by {existing.spec.id!r}")
        owner = self._intents.get((spec.capability, spec.intent))
        if owner is not None and owner != spec.id:
            raise ValueError(f"{spec.capability}.{spec.intent} is already served by {owner!r}")

    def add_native(self, spec: ToolSpec | WireSpec, handler: NativeHandler) -> None:
        """Register one native tool.

        A ``ToolSpec`` carries the full identity; a bare ``WireSpec`` is
        accepted for small callers and tests, deriving ``capability.intent``
        from the wire name. The production path always passes a ToolSpec.
        """
        if isinstance(spec, WireSpec):
            spec = self._from_wire(spec)
        self._claim(spec, kind="native")
        self._tools[spec.id] = RegisteredTool(spec, "native", handler)
        self._intents[(spec.capability, spec.intent)] = spec.id
        self._index_aliases(spec)

    def add_provider(
        self,
        spec: WireSpec,
        handler: NativeHandler,
        *,
        effect: Literal["read", "write", "destructive"],
        source: str,
        auth_mode: Literal["oauth", "token"],
        intent: str | None = None,
    ) -> None:
        """Register one direct-provider action.

        The wire name is ``app__action``; the model-facing id is
        ``app.action``. The provider's declared effect maps to the risk
        vocabulary; the policy still checks the effect itself.
        """
        legacy = spec.name
        action = intent or legacy.split("__", 1)[-1]
        capability = _PROVIDER_CAPABILITY.get(source, source)
        risk: Literal["read", "write", "external"] = (
            "read" if effect == "read" else "write" if effect == "write" else "external"
        )
        rich = ToolSpec(
            id=f"{capability}.{action}",
            capability=capability,
            intent=action,
            summary=_summarise(spec.description, f"{source} {action}"),
            description=spec.description or f"{source} {action}",
            risk=risk,
            raw_schema=spec.input_schema,
            backends=(source,),
            aliases=(legacy,),
        )
        self._claim(rich, kind="provider")
        self._tools[rich.id] = RegisteredTool(
            rich, "provider", handler, effect=effect, auth_mode=auth_mode
        )
        self._intents[(rich.capability, rich.intent)] = rich.id
        self._index_aliases(rich)

    def attach_mcp(self, host: MCPHost) -> None:
        self._mcp_host = host

    def attach_mail_filter(self, mail_filter: MailAllowlist) -> None:
        self._mail_filter = mail_filter

    def sync_mcp_tools(self) -> int:
        """Pull the host's current tool list into the namespace.

        A server's tool is ``server.tool``; its capability is the server name
        and its backend is the server, so health can hide it when the
        watchdog says the server is down.
        """
        if self._mcp_host is None:
            return 0
        added = 0
        reported: dict[str, set[str]] = {}
        for wire in self._mcp_host.tool_specs():
            server, sep, action = wire.name.partition("__")
            if not sep:
                server, action = wire.source, wire.name
            rich = ToolSpec(
                id=f"{server}.{action}",
                capability=server,
                intent=action,
                summary=_summarise(wire.description, f"{server} {action}"),
                description=wire.description or f"{server} {action}",
                risk=_risk_for_mcp(action),
                raw_schema=wire.input_schema,
                backends=(server,),
                aliases=(wire.name,),
            )
            reported.setdefault(server, set()).add(rich.id)
            existing = self._tools.get(rich.id)
            if existing is not None and existing.kind in ("native", "provider"):
                log.warning(
                    "mcp tool %s is shadowed by an existing %s tool", rich.id, existing.kind
                )
                continue
            owner = self._intents.get((rich.capability, rich.intent))
            if owner is not None and owner != rich.id:
                log.warning("mcp tool %s collides with %s — skipped", rich.id, owner)
                continue
            if existing is None:
                added += 1
            self._tools[rich.id] = RegisteredTool(rich, "mcp")
            self._intents[(rich.capability, rich.intent)] = rich.id
            self._index_aliases(rich)
        for server, ids in reported.items():
            for tool_id in list(self._tools):
                tool = self._tools[tool_id]
                if tool.kind == "mcp" and tool.spec.capability == server and tool_id not in ids:
                    del self._tools[tool_id]
                    self._intents.pop((tool.spec.capability, tool.spec.intent), None)
                    self._drop_aliases(tool.spec)
                    log.info("mcp tool %s left the namespace", tool_id)
        return added

    def _index_aliases(self, spec: ToolSpec) -> None:
        for alias in (spec.id, spec.wire_name, *spec.aliases):
            if alias:
                self._aliases[alias] = spec.id

    @staticmethod
    def _from_wire(wire: WireSpec) -> ToolSpec:
        capability, sep, intent = wire.name.partition("__")
        if not sep:
            capability, intent = "native", wire.name
        return ToolSpec(
            id=f"{capability}.{intent}",
            capability=capability,
            intent=intent,
            summary=_summarise(wire.description, intent),
            description=wire.description or intent,
            risk="external",
            raw_schema=wire.input_schema,
            aliases=(wire.name,),
            core=True,
            # a bare WireSpec is a test or small-caller registration, never
            # the production path; it carries no declaration and follows the
            # legacy classifier, exactly as before manifests existed
        )

    # -- visibility ------------------------------------------------------------

    def _healthy(self, spec: ToolSpec) -> bool:
        return all(self.health.healthy(backend) for backend in spec.backends)

    def available(self, id_or_alias: str) -> bool:
        tool = self.resolve(id_or_alias)
        if tool is None:
            return False
        return self.grants.granted(tool.spec.capability) and self._healthy(tool.spec)

    def visible(self) -> list[ToolSpec]:
        """Granted and healthy tools, in registration order."""
        return [
            tool.spec
            for tool in self._tools.values()
            if self.grants.granted(tool.spec.capability) and self._healthy(tool.spec)
        ]

    def index(self) -> str:
        """The prompt index: one line per visible tool, nothing else."""
        return "\n".join(spec.index_line() for spec in self.visible())

    def core_specs(self) -> list[WireSpec]:
        return [spec.wire() for spec in self.visible() if spec.core]

    def schemas(self, ids: set[str]) -> list[WireSpec]:
        """The loaded ids' schemas, in registration order.

        Order matters: a provider's tool list is stable, and a deterministic
        prompt keeps provider-side caching honest.
        """
        wanted: set[str] = set()
        for raw in ids:
            tool = self.resolve(str(raw))
            if tool is not None:
                wanted.add(tool.spec.id)
        return [
            tool.wire
            for tool in self._tools.values()
            if tool.spec.id in wanted and self.available(tool.spec.id)
        ]

    def load(self, ids: list[str], loaded: set[str]) -> tuple[list[str], str]:
        """Load schemas for this turn, at most ``max_loaded`` new ids.

        Loading is not a grant: an ungranted or unhealthy tool is refused
        here exactly as it would be by the executor. The cap counts the
        tools already loaded this turn, so a model cannot walk the whole
        catalog through the prompt in one turn.
        """
        accepted: list[str] = []
        refused: list[str] = []
        present: list[str] = []
        for raw in ids:
            tool = self.resolve(str(raw))
            if tool is None or not self.available(tool.spec.id):
                refused.append(str(raw))
                continue
            if tool.spec.core:
                # a core tool's schema already rides the turn; loading it
                # would waste one of the eight slots
                present.append(tool.spec.id)
                continue
            if tool.spec.id in loaded or tool.spec.id in accepted:
                present.append(tool.spec.id)
                continue
            if len(loaded) + len(accepted) >= self.max_loaded:
                refused.append(str(raw))
                continue
            accepted.append(tool.spec.id)
        lines = []
        if accepted:
            lines.append("Loaded: " + ", ".join(self._display_name(i) for i in accepted))
        if present:
            lines.append("Already loaded: " + ", ".join(self._display_name(i) for i in present))
        if refused:
            lines.append("Not available this turn: " + ", ".join(refused))
        return accepted, "\n".join(lines) or "Nothing loaded."

    def _display_name(self, tool_id: str) -> str:
        tool = self.resolve(tool_id)
        return tool.spec.wire_name if tool is not None else tool_id

    # -- use ------------------------------------------------------------------

    def resolve(self, id_or_alias: str) -> RegisteredTool | None:
        tool = self._tools.get(id_or_alias)
        if tool is not None:
            return tool
        target = self._aliases.get(id_or_alias)
        return self._tools.get(target) if target else None

    def aliases_for(self, id_or_alias: str) -> list[str]:
        """Every other name this tool answers to — the raw name first."""
        tool = self.resolve(id_or_alias)
        if tool is None:
            return []
        names = (tool.spec.id, tool.spec.wire_name, *tool.spec.aliases)
        return [name for name in names if name and name != id_or_alias]

    def is_internal(self, id_or_alias: str) -> bool:
        """True for Aether's own tools — not a provider app, not an MCP
        server that happens to share a capability's name."""
        tool = self.resolve(id_or_alias)
        return bool(
            tool is not None
            and tool.kind == "native"
            and tool.spec.capability in INTERNAL_CAPABILITIES
            and tool.spec.capability != "web"
        )

    def _drop_aliases(self, spec: ToolSpec) -> None:
        for alias in (spec.id, spec.wire_name, *spec.aliases):
            if alias and self._aliases.get(alias) == spec.id:
                del self._aliases[alias]

    def specs(self) -> list[WireSpec]:
        return [tool.wire for tool in self._tools.values()]

    def defs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def search(self, query: str, limit: int = 8) -> list[ToolSpec]:
        """Keyword search over id, intent and summary, visible tools only."""
        words = _tokens(query)
        if not words:
            return []
        scored: list[tuple[int, ToolSpec]] = []
        for spec in self.visible():
            haystack = f"{spec.id} {spec.intent} {spec.summary}"
            hits = len(words & _tokens(haystack))
            if hits:
                scored.append((hits, spec))
        scored.sort(key=lambda pair: -pair[0])
        return [spec for _, spec in scored[:limit]]

    def get(self, name: str) -> RegisteredTool | None:
        return self.resolve(name)

    def has_mcp_server(self, server: str) -> bool:
        """True when this MCP server contributed any tool to the namespace."""
        return any(
            tool.kind == "mcp" and tool.spec.capability == server for tool in self._tools.values()
        )

    def has_server(self, server: str) -> bool:
        """Whether any tool answers for this app or server name — a
        capability for a native or provider tool, a backend for the app key
        behind it, or an MCP server."""
        return any(
            tool.spec.capability == server or server in tool.spec.backends
            for tool in self._tools.values()
        )

    def mcp_servers(self) -> list[str]:
        return sorted({tool.spec.capability for tool in self._tools.values() if tool.kind == "mcp"})

    def mcp_status(self) -> dict[str, bool]:
        if self._mcp_host is None:
            return {}
        return {conn.name: conn.ready for conn in self._mcp_host.connections}

    def server_action_count(self, server: str) -> int:
        return sum(
            1
            for tool in self._tools.values()
            if tool.kind == "mcp" and tool.spec.capability == server
        )

    def drop_server(self, server: str) -> int:
        ids = [
            tool_id
            for tool_id, tool in self._tools.items()
            if tool.kind == "mcp" and tool.spec.capability == server
        ]
        for tool_id in ids:
            tool = self._tools.pop(tool_id)
            self._intents.pop((tool.spec.capability, tool.spec.intent), None)
            self._drop_aliases(tool.spec)
        if ids:
            log.info("dropped %d tool(s) from mcp server %r", len(ids), server)
        return len(ids)

    def drop_app(self, app: str) -> int:
        ids = [
            tool_id
            for tool_id, tool in self._tools.items()
            if tool.kind == "provider" and app in tool.spec.backends
        ]
        for tool_id in ids:
            tool = self._tools.pop(tool_id)
            self._intents.pop((tool.spec.capability, tool.spec.intent), None)
            self._drop_aliases(tool.spec)
        if ids:
            log.info("dropped %d provider tool(s) from %r", len(ids), app)
        return len(ids)

    def drop_provider_tools(self) -> int:
        ids = [tool_id for tool_id, tool in self._tools.items() if tool.kind == "provider"]
        for tool_id in ids:
            tool = self._tools.pop(tool_id)
            self._intents.pop((tool.spec.capability, tool.spec.intent), None)
            self._drop_aliases(tool.spec)
        return len(ids)

    def native_names(self) -> list[str]:
        """Every registered native tool's wire name, in stable order."""
        return sorted(tool.spec.wire_name for tool in self._tools.values() if tool.kind == "native")

    def provider_apps(self) -> list[str]:
        return sorted(
            {
                backend
                for tool in self._tools.values()
                if tool.kind == "provider"
                for backend in tool.spec.backends
            }
        )

    def provider_action_count(self, app: str) -> int:
        return sum(
            1
            for tool in self._tools.values()
            if tool.kind == "provider" and app in tool.spec.backends
        )

    def __len__(self) -> int:
        return len(self._tools)

    # -- use ------------------------------------------------------------------

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        tool = self.resolve(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool: {name}")
        # hiding is not the security boundary — authz is. But a tool that is
        # ungranted or whose backend is down must fail the same way whether
        # the model reached it through the prompt or a stale approval.
        if not self.available(tool.spec.id):
            raise UnknownToolError(f"tool not available: {name}")
        wire_name = tool.spec.wire_name
        if tool.kind in ("native", "provider"):
            assert tool.handler is not None
            result = _stringify(await tool.handler(params or {}))
        else:
            if self._mcp_host is None:
                raise UnknownToolError(f"mcp tool {name} has no host to route through")
            result = await self._mcp_host.call(wire_name, params or {})
        if self._mail_filter is not None and tool.kind in ("provider", "mcp"):
            result = await self._mail_filter.filter_result(wire_name, result)
        return result


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)
