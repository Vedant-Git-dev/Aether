"""/pause and /resume — the background worker's chat switch: the user's
own word is the approval, so the write rides the same executor path a
/config line takes (classified, audited, persisted through the config
manager), the already-state answers honestly, and no instance of the
model is ever woken for it."""

from __future__ import annotations

from fakes import FakeConfigManager, FakeProvider
from test_agent_loop import LoopKit, _msg

from aether.config import AppConfig, ProactiveConfig

PAUSED = "the background worker is already off — say /resume when you want it back."
RUNNING = "the background worker is already running — say /pause to stop it."
UNWIRED = (
    "config management isn't wired on this instance, so there's no background worker to switch."
)


async def test_pause_when_already_off_answers_honestly() -> None:
    provider = FakeProvider([])  # any LLM call in a command test is a bug
    manager = FakeConfigManager()
    kit = LoopKit(provider, config_manager=manager)  # enabled=False by default
    kit.loop.submit_message(_msg("/pause"))
    await kit.loop._tick()

    assert kit.connector.sent == [PAUSED]
    assert manager.set_calls == []  # nothing to write — the state was already true
    assert provider.calls == []
    assert kit.events.ingested == []


async def test_pause_writes_the_switch_through_the_gate() -> None:
    provider = FakeProvider([])
    manager = FakeConfigManager(set_reply="background work paused")
    config = AppConfig(proactive=ProactiveConfig(enabled=True))
    kit = LoopKit(provider, config_manager=manager, config=config)
    kit.loop.submit_message(_msg("/pause"))
    await kit.loop._tick()

    # one synthetic set_config call — classified tuning, audited, applied
    assert manager.set_calls == [
        {"op": "set", "path": "proactive.enabled", "value": False, "source": "tool"}
    ]
    assert kit.connector.sent == ["background work paused"]
    row = kit.audit.entries[-1]
    assert row["tool_name"] == "set_config"
    assert row["decision"] == "allow"
    assert row["rules_matched"] == "builtin:config-tune"
    assert provider.calls == []
    assert kit.events.ingested == []


async def test_resume_writes_the_switch_through_the_gate() -> None:
    provider = FakeProvider([])
    manager = FakeConfigManager(set_reply="background work resumed")
    kit = LoopKit(provider, config_manager=manager)  # off by default
    kit.loop.submit_message(_msg("/resume"))
    await kit.loop._tick()

    assert manager.set_calls == [
        {"op": "set", "path": "proactive.enabled", "value": True, "source": "tool"}
    ]
    assert kit.connector.sent == ["background work resumed"]
    assert provider.calls == []


async def test_resume_when_already_running_answers_honestly() -> None:
    provider = FakeProvider([])
    manager = FakeConfigManager()
    config = AppConfig(proactive=ProactiveConfig(enabled=True))
    kit = LoopKit(provider, config_manager=manager, config=config)
    kit.loop.submit_message(_msg("/resume"))
    await kit.loop._tick()

    assert kit.connector.sent == [RUNNING]
    assert manager.set_calls == []
    assert provider.calls == []


async def test_without_a_config_manager_there_is_nothing_to_switch() -> None:
    provider = FakeProvider([])
    kit = LoopKit(provider)  # no config manager wired — the honest reply
    kit.loop.submit_message(_msg("/pause"))
    await kit.loop._tick()

    assert kit.connector.sent == [UNWIRED]
    assert provider.calls == []
    assert kit.events.ingested == []
