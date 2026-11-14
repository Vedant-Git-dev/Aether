"""The tool layer end to end: the prompt index, on-demand schemas, the
executor's re-check, and the policy's view of the new names."""

from __future__ import annotations

from fakes import FakeProvider
from test_agent_loop import LoopKit, _msg

from aether.authz.policy import Decision, Policy
from aether.config import AuthzRule
from aether.connectors.registry import ToolRegistry
from aether.llm.types import ToolCall, Turn
from aether.tools.grants import BackendHealth
from aether.tools.model import ToolSpec


def _add_loader(kit: LoopKit) -> None:
    async def handler(params: dict) -> str:
        return "handled by the turn's loader"

    kit.tools.add_native(
        ToolSpec(
            id="tools.load",
            capability="tools",
            intent="load",
            summary="Load the full schemas for tool names.",
            description="Load the full schemas for tool names.",
            core=True,
        ),
        handler,
    )


def _add_mail(kit: LoopKit) -> None:
    async def handler(params: dict) -> str:
        kit.executed.append(("mail__read", dict(params)))
        return "2 unread: alice, bob"

    kit.tools.add_native(
        ToolSpec(
            id="mail.read",
            capability="mail",
            intent="read",
            summary="Read the mail.",
            description="Read the mail.",
            backends=("mail",),
        ),
        handler,
    )


async def test_a_deferred_tool_is_named_but_not_offered_until_loaded() -> None:
    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="l1",
                        name="tools__load",
                        arguments={"ids": ["mail__read"]},
                    )
                ]
            ),
            Turn(tool_calls=[ToolCall(id="r1", name="mail__read", arguments={})]),
            Turn(text="done"),
        ]
    )
    kit = LoopKit(provider)
    _add_loader(kit)
    _add_mail(kit)

    kit.loop.submit_message(_msg("read my mail"))
    await kit.loop._tick()

    first = {tool.name for tool in provider.calls[0][2]}
    assert "message__send" in first  # a terminal outcome is always ready
    assert "tools__load" in first
    assert "mail__read" not in first  # named in the index, not schematized
    assert "mail__read" in provider.calls[0][0]
    assert "not loaded yet" in provider.calls[0][0]
    # the load lands before the next completion, and the call runs
    assert "mail__read" in {tool.name for tool in provider.calls[1][2]}
    assert kit.executed == [("mail__read", {})]

    calls = kit.traces.created[-1]["payload"]["calls"]
    loader = next(call for call in calls if call["name"] == "tools__load")
    assert loader["decision"] == "allow"
    assert "mail__read" in loader["result"]


async def test_a_deferred_tool_called_without_loading_is_refused_not_run() -> None:
    provider = FakeProvider(
        [
            Turn(tool_calls=[ToolCall(id="r1", name="mail__read", arguments={})]),
            Turn(text="done"),
        ]
    )
    kit = LoopKit(provider)
    _add_loader(kit)
    _add_mail(kit)

    kit.loop.submit_message(_msg("read my mail"))
    await kit.loop._tick()

    assert kit.executed == []
    calls = kit.traces.created[-1]["payload"]["calls"]
    refused = next(call for call in calls if call["name"] == "mail__read")
    assert refused["is_error"] is True
    assert "isn't loaded this turn" in refused["result"]
    assert "tools__load" in refused["result"]


async def test_the_executor_refuses_an_unhealthy_backend_even_when_the_tool_is_loaded() -> None:
    kit = LoopKit(None)
    _add_mail(kit)
    kit.tools.health = BackendHealth(lambda backend: backend != "mail")

    result = await kit.loop._execute(
        ToolCall(id="r1", name="mail__read", arguments={}), origin=None
    )

    assert result.is_error is True
    assert "that action isn't available in mail right now" in result.content
    assert kit.executed == []


async def test_the_new_message_names_are_accepted_as_terminal_outcomes() -> None:
    provider = FakeProvider(
        [
            Turn(
                tool_calls=[
                    ToolCall(
                        id="r1",
                        name="message__reply",
                        arguments={"reply": "Here is the answer."},
                    )
                ]
            )
        ]
    )
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("first question"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer."]
    assert "terminal_miss" not in kit.traces.created[-1]["payload"]


async def test_a_solicited_turn_without_a_terminal_outcome_records_the_miss() -> None:
    provider = FakeProvider([Turn(text="Here is the answer.")])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("first question"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Here is the answer."]
    assert kit.traces.created[-1]["payload"]["terminal_miss"] is True


def test_a_user_rule_still_matches_the_old_name_of_a_renamed_tool() -> None:
    policy = Policy([AuthzRule(tool_pattern="gmail__send_email", decision="allow", note="my rule")])
    ruling = policy.classify(
        "email__send_email",
        effect="write",
        aliases=("gmail__send_email",),
    )
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "user:gmail__send_email"


def test_the_policy_sees_new_internal_names_as_internal() -> None:
    policy = Policy([])
    assert policy.classify("alerts__delete", internal=True).decision is Decision.ALLOW
    assert policy.classify("alerts__delete", internal=True).matched_rule == "builtin:internal"
    assert (
        policy.classify("alerts__create", {"scope": "mail"}, internal=True).matched_rule
        == "builtin:internal"
    )
    # the same name without the registry's word is just a risky verb — an
    # MCP server called alerts must never inherit Aether's own trust
    assert policy.classify("alerts__delete", internal=False).decision is (Decision.REQUIRE_APPROVAL)


def test_an_alert_offer_parks_for_the_user() -> None:
    policy = Policy([])

    offered = policy.classify("alerts__offer", {"scope": "mail"}, internal=True)
    direct = policy.classify(
        "alerts__create", {"title": "landlord", "scope": "mail"}, internal=True
    )

    # an offer is always a card: no path arms a spotted alert silently
    assert offered.decision is Decision.REQUIRE_APPROVAL
    assert offered.matched_rule == "builtin:alert-offer"
    # a direct create does not park here — the tool layer reads the turn's
    # provenance, so content that was read can never arm one
    assert direct.decision is Decision.ALLOW


def test_an_alert_tool_is_registered_as_internal() -> None:
    registry = ToolRegistry()

    async def handler(params: dict) -> str:
        return "ok"

    registry.add_native(
        ToolSpec(
            id="alerts.create",
            capability="alerts",
            intent="create",
            summary="Set an alert.",
            description="Set an alert.",
            risk="write",
            raw_schema={"type": "object", "properties": {}},
            aliases=("alerts__create",),
        ),
        handler,
    )

    assert registry.is_internal("alerts__create") is True
    assert registry.is_internal("alerts__delete") is False  # not on this spec
