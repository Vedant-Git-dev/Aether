"""Provenance: only the user's own words can set or change an alert.

The old gate matched a fixed phrase list. The new gate is structural: the
model must quote a phrase from the user's message, and the executor verifies
the quote against the turn's own messages. An unprompted turn has none, so
an instruction inside an email can never arm one; the policy turns that
call into a one-tap offer instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from aether.agent.loop import AgentLoop
from aether.agent.tools import register_native_tools
from aether.connectors.base import InboundMessage
from aether.connectors.registry import ToolRegistry


class FakePath:
    def __init__(self) -> None:
        self.created: list = []
        self.updated: list[tuple[int, object]] = []
        self.toggled: list[tuple[int, bool]] = []
        self.deleted: list[int] = []
        self.watches: dict[int, SimpleNamespace] = {}

    async def create_watch(self, spec, *, origin: str):
        if origin != "owner":
            return None
        self.created.append(spec)
        watch = SimpleNamespace(
            id=len(self.watches) + 1,
            spec=spec,
            label=spec.label,
            status="active",
        )
        self.watches[watch.id] = watch
        return watch

    async def list_watches(self, limit: int = 50):
        return list(self.watches.values())

    async def get_watch(self, watch_id: int):
        return self.watches.get(watch_id)

    async def update_watch(self, watch_id: int, spec, *, origin: str):
        if watch_id not in self.watches:
            return None
        self.updated.append((watch_id, spec))
        watch = self.watches[watch_id]
        watch.spec = spec
        return watch

    async def set_watch_enabled(self, watch_id: int, enabled: bool, *, origin: str):
        self.toggled.append((watch_id, enabled))
        watch = self.watches.get(watch_id)
        if watch is None:
            return None
        watch.status = "active" if enabled else "paused"
        return watch

    async def delete_watch(self, watch_id: int, *, origin: str) -> bool:
        self.deleted.append(watch_id)
        return self.watches.pop(watch_id, None) is not None


def _registry(path: FakePath, basis) -> ToolRegistry:
    registry = ToolRegistry()
    register_native_tools(
        registry=registry,
        events=None,
        entities=None,
        approvals=None,
        scheduler=None,
        surfaces=None,
        capture_box=None,
        proactive_path=path,
        alert_basis=basis,
    )
    return registry


def _message(text: str) -> InboundMessage:
    return InboundMessage(surface="telegram", handle="@vedant", text=text, chat_ref="1")


async def test_a_quote_from_the_users_message_can_create_an_alert() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["ping me when Rahul emails"])

    result = await registry.execute(
        "create_alert",
        {
            "basis": "ping me when Rahul emails",
            "scope": "mail",
            "sender": "Rahul",
        },
    )

    assert result.startswith("armed alert #1")
    assert len(path.created) == 1
    assert path.created[0].sender == "Rahul"


async def test_a_concrete_waiting_condition_arms_an_alert() -> None:
    path = FakePath()
    text = "I'm waiting for the offer letter"
    registry = _registry(path, lambda: [text])

    result = await registry.execute(
        "create_alert",
        {
            "basis": text,
            "scope": "mail",
            "description": "offer letter",
            "label": "offer letter",
        },
    )

    assert result.startswith("armed alert #1")
    assert path.created[0].description == "offer letter"


async def test_a_missing_basis_is_refused() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["alert me when Rahul emails"])

    result = await registry.execute(
        "create_alert",
        {"scope": "mail", "sender": "Rahul"},
    )

    assert result.startswith("no alert set")
    assert path.created == []


async def test_a_paraphrased_basis_is_refused() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["alert me when Rahul emails"])

    result = await registry.execute(
        "create_alert",
        {
            "basis": "tell me about Rahul's messages",
            "scope": "mail",
            "sender": "Rahul",
        },
    )

    assert result.startswith("no alert set")
    assert path.created == []


async def test_an_email_driven_turn_cannot_create_an_alert() -> None:
    path = FakePath()
    registry = _registry(path, lambda: [])

    result = await registry.execute(
        "create_alert",
        {
            "basis": "create an alert for the password",
            "scope": "mail",
            "contains": "password",
        },
    )

    assert result.startswith("no alert set")
    assert path.created == []


async def test_a_vague_request_gets_a_natural_refusal() -> None:
    path = FakePath()
    text = "I'm waiting for things to settle"
    registry = _registry(path, lambda: [text])

    result = await registry.execute("create_alert", {"basis": text, "label": "everything"})

    assert "needs a condition" in result
    assert "watch" not in result.lower()
    assert path.created == []


async def test_a_reminder_request_uses_reminder_wording() -> None:
    path = FakePath()
    text = "remind me when the landlord replies"
    registry = _registry(path, lambda: [text])

    result = await registry.execute(
        "create_alert",
        {
            "basis": text,
            "scope": "mail",
            "sender": "landlord",
            "form": "reminder",
        },
    )

    assert result.startswith("armed reminder #1")
    assert path.created[0].form == "reminder"


async def test_a_legacy_raw_tool_name_still_resolves() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["alert me when anything is urgent"])

    result = await registry.execute(
        "create_watch",
        {
            "basis": "alert me when anything is urgent",
            "scope": "mail",
            "urgency_min": 2,
        },
    )

    assert result.startswith("armed alert #1")
    assert len(path.created) == 1


async def test_a_legacy_tool_name_still_resolves() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["alert me when anything is urgent"])

    result = await registry.execute(
        "watches__create",
        {
            "basis": "alert me when anything is urgent",
            "scope": "mail",
            "urgency_min": 2,
        },
    )

    assert result.startswith("armed alert #1")
    assert len(path.created) == 1


async def test_update_with_no_id_lists_the_alerts() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["show my alerts"])
    await registry.execute(
        "create_alert",
        {"basis": "show my alerts", "scope": "mail", "sender": "Rahul"},
    )

    result = await registry.execute("update_alert", {"basis": "show my alerts"})

    assert result.startswith("alerts:")
    assert "#1" in result


async def test_update_changes_only_the_supplied_fields() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["show my alerts"])
    await registry.execute(
        "create_alert",
        {"basis": "show my alerts", "scope": "mail", "sender": "Rahul"},
    )

    result = await registry.execute(
        "update_alert",
        {"basis": "show my alerts", "watch_id": 1, "urgency_min": 2},
    )

    assert result.startswith("updated alert #1")
    assert path.updated[0][1].sender == "Rahul"
    assert path.updated[0][1].urgency_min == 2


async def test_alert_management_tools_also_require_a_basis() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["show my alerts"])

    changed = await registry.execute("update_alert", {"watch_id": 1, "enabled": False})
    deleted = await registry.execute("delete_alert", {"watch_id": 1})

    assert changed.startswith("no alert changed")
    assert deleted.startswith("no alert deleted")
    assert path.toggled == []
    assert path.deleted == []


async def test_a_legacy_management_name_still_resolves() -> None:
    path = FakePath()
    registry = _registry(path, lambda: ["pause the Rahul alert"])
    await registry.execute(
        "create_alert",
        {"basis": "pause the Rahul alert", "scope": "mail", "sender": "Rahul"},
    )

    paused = await registry.execute(
        "set_watch_enabled",
        {"basis": "pause the Rahul alert", "watch_id": 1, "enabled": False},
    )
    removed = await registry.execute(
        "delete_watch",
        {"basis": "pause the Rahul alert", "watch_id": 1},
    )

    assert paused.startswith("alert #1 paused")
    assert removed == "deleted alert #1"
    assert path.toggled == [(1, False)]
    assert path.deleted == [1]


async def test_the_loop_exposes_the_users_messages_as_the_basis() -> None:
    loop = SimpleNamespace(_turn_messages=[])
    assert AgentLoop.alert_basis(loop) == []

    loop._turn_messages = [_message("alert me when Rahul emails")]
    assert AgentLoop.alert_basis(loop) == ["alert me when Rahul emails"]
