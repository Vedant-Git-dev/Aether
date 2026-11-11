"""ToolRegistry tests — flat namespace, dispatch, shadowing. All fakes."""

from __future__ import annotations

import json

import pytest
from fakes import FakeAudit

from aether.config import AppContacts, ContactRule, ContactsConfig
from aether.connectors.base import UnknownToolError
from aether.connectors.mail_allowlist import MailAllowlist
from aether.connectors.registry import RegisteredTool, ToolRegistry
from aether.llm.types import WireSpec
from aether.tools.grants import BackendHealth, CapabilityGrants
from aether.tools.model import ToolSpec


class FakeHost:
    """MCPHost double: canned specs, recorded calls."""

    def __init__(self, specs: list[WireSpec]) -> None:
        self.specs_list = specs
        self.calls: list[tuple[str, dict]] = []

    def tool_specs(self) -> list[WireSpec]:
        return self.specs_list

    async def call(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return f"mcp:{name}"


def _native(name: str, source: str = "native") -> WireSpec:
    return WireSpec(name=name, description=f"native {name}", source=source)


async def _echo_handler(params: dict) -> dict:
    return {"echo": params.get("text", "")}


async def test_native_tools_execute_and_stringify() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("memory_search"), _echo_handler)

    spec = registry.specs()[0]
    assert spec.source == "native"  # the default
    assert await registry.execute("memory_search", {"text": "hi"}) == '{"echo": "hi"}'


async def test_native_string_results_pass_through() -> None:
    async def plain(params: dict) -> str:
        return "kept as-is"

    registry = ToolRegistry()
    registry.add_native(_native("plain"), plain)
    assert await registry.execute("plain", {}) == "kept as-is"


async def test_duplicate_native_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("t"), _echo_handler)
    with pytest.raises(ValueError):
        registry.add_native(_native("t"), _echo_handler)


async def test_mcp_tools_sync_into_the_namespace() -> None:
    registry = ToolRegistry()
    host = FakeHost([WireSpec(name="mail__list_unread", description="unread mail", source="mail")])
    registry.attach_mcp(host)
    assert registry.sync_mcp_tools() == 1
    assert registry.sync_mcp_tools() == 0  # idempotent

    spec = registry.get("mail__list_unread")
    assert spec is not None and spec.kind == "mcp"
    assert registry.specs()[0].source == "mail"

    assert await registry.execute("mail__list_unread", {"limit": 2}) == "mcp:mail__list_unread"
    assert host.calls == [("mail__list_unread", {"limit": 2})]


async def test_native_tools_shadow_same_named_mcp_tools() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("mail__list_unread"), _echo_handler)
    host = FakeHost([WireSpec(name="mail__list_unread", description="mcp one", source="mail")])
    registry.attach_mcp(host)
    registry.sync_mcp_tools()

    assert await registry.execute("mail__list_unread", {"text": "x"}) == '{"echo": "x"}'
    assert host.calls == []  # the native tool won


async def test_unknown_tools_do_not_execute() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError):
        await registry.execute("ghost", {})


# -- the mail filter rides every mcp result ---------------------------------------


class GmailHost(FakeHost):
    """MCPHost double that answers gmail calls with a canned batch."""

    def __init__(self, batch: str) -> None:
        super().__init__(
            [WireSpec(name="gmail__list_messages", description="mail", source="gmail")]
        )
        self.batch = batch

    async def call(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return self.batch


async def test_attached_mail_filter_trims_mcp_gmail_results() -> None:
    # execute is the one dispatch point for the model's own calls and
    # approved replays — the filter must ride it, not sit beside it
    batch = json.dumps(
        {
            "successful": True,
            "data": {
                "messages": [
                    {"sender": "abc123@xyz.com", "to": "me@example.com", "subject": "keep"},
                    {
                        "sender": "Newsletter <noreply@digest.example.com>",
                        "to": "me@example.com",
                        "subject": "drop",
                    },
                ]
            },
        }
    )
    registry = ToolRegistry()
    contacts = ContactsConfig(
        mode="enforce",
        gmail=AppContacts(allowlist=[ContactRule(platform="gmail", handle="abc123@xyz.com")]),
    )
    registry.attach_mcp(GmailHost(batch))
    registry.attach_mail_filter(MailAllowlist(contacts, FakeAudit()))
    registry.sync_mcp_tools()

    out = json.loads(await registry.execute("gmail__list_messages", {}))
    assert [m["subject"] for m in out["data"]["messages"]] == ["keep"]


async def test_no_filter_attached_leaves_results_untouched() -> None:
    batch = json.dumps(
        {"successful": True, "data": {"messages": [{"sender": "anyone@example.com"}]}}
    )
    registry = ToolRegistry()
    registry.attach_mcp(GmailHost(batch))
    registry.sync_mcp_tools()
    out = json.loads(await registry.execute("gmail__list_messages", {}))
    assert len(out["data"]["messages"]) == 1


async def test_mcp_tool_without_host_is_an_error() -> None:
    # defensive branch: an mcp entry can only exist via sync_mcp_tools, which
    # requires a host — but if one ever routes with no host attached it
    # must fail loudly, not silently do nothing
    registry = ToolRegistry()
    registry.add_native(_native("t"), _echo_handler)
    spec = ToolSpec(
        id="srv.x",
        capability="srv",
        intent="x",
        summary="An MCP tool with no host",
        description="",
    )
    registry._tools[spec.id] = RegisteredTool(spec, "mcp")
    registry._index_aliases(spec)
    with pytest.raises(UnknownToolError):
        await registry.execute("srv__x", {})


async def test_non_gmail_mcp_tools_are_untouched() -> None:
    registry = ToolRegistry()
    host = FakeHost([WireSpec(name="mail__send_message", description="send", source="mail")])
    registry.attach_mcp(host)
    registry.sync_mcp_tools()
    await registry.execute("mail__send_message", {"body": "hi\nthere"})
    assert host.calls[0][1] == {"body": "hi\nthere"}


async def test_drop_server_removes_only_that_servers_tools() -> None:
    """The other half of sync_mcp_tools: an app unlinked or disabled from
    chat must not keep its actions callable — and nobody else's drop."""
    registry = ToolRegistry()
    registry.add_native(_native("memory_search"), _echo_handler)
    host = FakeHost(
        [
            WireSpec(name="mail__list_unread", description="a", source="mail"),
            WireSpec(name="mail__send_email", description="b", source="mail"),
            WireSpec(name="calendar__get_events", description="c", source="calendar"),
        ]
    )
    registry.attach_mcp(host)
    assert registry.sync_mcp_tools() == 3
    assert len(registry) == 4

    assert registry.drop_server("mail") == 2
    assert len(registry) == 2
    assert registry.get("mail__list_unread") is None
    assert registry.get("mail__send_email") is None
    with pytest.raises(UnknownToolError):
        await registry.execute("mail__send_email", {})  # gone means not callable
    # the neighbors and the natives are untouched
    assert registry.get("calendar__get_events") is not None
    assert registry.get("memory_search") is not None
    # the __ delimiter keeps a prefixing name from over-matching
    assert registry.drop_server("cal") == 0
    assert registry.get("calendar__get_events") is not None
    # and dropping what's already gone is a no-op
    assert registry.drop_server("mail") == 0


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add_native(_native("memory_search"), _echo_handler)
    host = FakeHost(
        [
            WireSpec(
                name="hub__GMAIL_SEND_EMAIL",
                description="Send an email to a recipient",
                source="hub",
            ),
            WireSpec(
                name="hub__CALENDAR_LIST_EVENTS",
                description="List events from your calendar",
                source="hub",
            ),
        ]
    )
    registry.attach_mcp(host)
    registry.sync_mcp_tools()
    return registry


def test_core_specs_are_always_offered_and_mcp_is_not() -> None:
    registry = _registry()
    assert [spec.name for spec in registry.core_specs()] == ["native__memory_search"]
    # the prompt index still names everything reachable
    assert "hub__CALENDAR_LIST_EVENTS" in registry.index()
    assert "hub__GMAIL_SEND_EMAIL" in registry.index()


def test_loading_is_capped_and_reports_every_refusal() -> None:
    registry = _registry()
    registry.max_loaded = 1
    loaded: set[str] = set()
    accepted, text = registry.load(["hub__CALENDAR_LIST_EVENTS", "hub__GMAIL_SEND_EMAIL"], loaded)
    assert accepted == ["hub.CALENDAR_LIST_EVENTS"]
    assert "Loaded:" in text
    assert "Not available this turn:" in text
    # loading the same id again is a no-op, not a refusal
    loaded.update(accepted)
    accepted, text = registry.load(["hub__CALENDAR_LIST_EVENTS"], loaded)
    assert accepted == []
    assert "Already loaded: hub__CALENDAR_LIST_EVENTS" in text
    # a core tool is already schematized and never spends a slot
    accepted, text = registry.load(["memory_search"], loaded)
    assert accepted == []
    assert "Already loaded: native__memory_search" in text
    # the loaded schema is the provider-safe name
    assert [spec.name for spec in registry.schemas(loaded)] == ["hub__CALENDAR_LIST_EVENTS"]


def test_visibility_hides_ungranted_and_unhealthy_tools() -> None:
    ungranted = ToolRegistry(grants=CapabilityGrants(lambda capability: capability != "hub"))
    ungranted.attach_mcp(FakeHost([WireSpec(name="hub__list", description="", source="hub")]))
    ungranted.sync_mcp_tools()
    assert ungranted.visible() == []

    unhealthy = ToolRegistry(health=BackendHealth(lambda backend: backend != "hub"))
    unhealthy.attach_mcp(FakeHost([WireSpec(name="hub__list", description="", source="hub")]))
    unhealthy.sync_mcp_tools()
    assert unhealthy.visible() == []


def test_a_duplicate_intent_names_the_tool_that_owns_it() -> None:
    registry = ToolRegistry()
    first = ToolSpec(
        id="mail.list",
        capability="mail",
        intent="list",
        summary="List the mail.",
        description="",
    )
    registry.add_native(first, _echo_handler)
    second = ToolSpec(
        id="mail.list_all",
        capability="mail",
        intent="list",
        summary="List all the mail.",
        description="",
    )
    with pytest.raises(ValueError, match="mail.list"):
        registry.add_native(second, _echo_handler)


def test_a_duplicate_id_is_rejected_even_for_a_native() -> None:
    registry = ToolRegistry()
    registry.add_native(_native("t"), _echo_handler)
    with pytest.raises(ValueError, match="native.t"):
        registry.add_native(_native("t"), _echo_handler)


def test_search_scores_token_overlap_not_substrings() -> None:
    registry = _registry()
    assert registry.search("a") == []  # one-char junk matches nothing
    assert registry.search("the a") == []  # stopword-only query matches nothing
    found = [s.wire_name for s in registry.search("calendar events")]
    assert found[0] == "hub__CALENDAR_LIST_EVENTS"  # both tokens hit
    assert "hub__GMAIL_SEND_EMAIL" not in found
