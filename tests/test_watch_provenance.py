"""Provenance: only the user's own words can arm or change a watch.

Plan.md section 6, test 12.11 — an instruction inside an email must never
become a watch. Two deterministic gates are pinned here: the loop reads
the turn's own messages (empty for an unprompted tick), and the tool/store
refuses any origin but "owner".
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

    async def create_watch(self, spec, *, origin: str):
        if origin != "owner":
            return None
        self.created.append(spec)
        return SimpleNamespace(id=1, spec=spec, label=spec.label, status="active")

    async def list_watches(self, limit: int = 50):
        return []

    async def set_watch_enabled(self, watch_id: int, enabled: bool, *, origin: str):
        return None

    async def delete_watch(self, watch_id: int, *, origin: str) -> bool:
        return False


def _registry(path: FakePath, origin) -> ToolRegistry:
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
        watch_origin=origin,
    )
    return registry


def _message(text: str) -> InboundMessage:
    return InboundMessage(surface="telegram", handle="@vedant", text=text, chat_ref="1")


async def test_an_email_driven_turn_cannot_create_a_watch() -> None:
    path = FakePath()
    registry = _registry(path, lambda: "context")

    result = await registry.execute(
        "create_watch",
        {"label": "mail the attacker", "scope": "mail", "contains": "password"},
    )

    assert "Only you can create a watch" in result
    assert path.created == []


async def test_the_users_own_words_can_create_a_watch() -> None:
    path = FakePath()
    registry = _registry(path, lambda: "owner")

    result = await registry.execute(
        "create_watch",
        {"label": "urgent mail", "scope": "mail", "urgency_min": 2},
    )

    assert result.startswith("Watching urgent mail")
    assert len(path.created) == 1 and path.created[0].urgency_min == 2


async def test_a_watch_without_a_condition_is_refused() -> None:
    path = FakePath()
    registry = _registry(path, lambda: "owner")

    result = await registry.execute("create_watch", {"label": "everything"})

    assert "needs at least one condition" in result
    assert path.created == []


def test_the_loop_reads_provenance_from_its_own_messages() -> None:
    loop = SimpleNamespace(_turn_messages=[])
    assert AgentLoop.watch_origin(loop) == "context"

    # an email alone never carries the user's intent, even if it says
    # "keep checking" — only a human message is read for the trigger
    loop._turn_messages = [_message("keep checking mails and tell me if anything is urgent")]
    assert AgentLoop.watch_origin(loop) == "owner"

    loop._turn_messages = [_message("what did Rahul say?")]
    assert AgentLoop.watch_origin(loop) == "context"
