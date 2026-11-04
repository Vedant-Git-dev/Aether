"""/profile — the deterministic front door to the user's profile: a one-
line seed goes straight into the workspace confirmed (no model, no ingest),
bare /profile gathers what the agent inferred into one batched
confirmation, one yes keeps it all, and a correction is normal chat that
leaves the guesses pending until the model supersedes them."""

from __future__ import annotations

import pytest
from fakes import FakeProvider
from test_agent_loop import LoopKit, _msg

from aether.llm.types import Turn
from aether.memory.crypto import Cipher, generate_key_b64
from aether.workspace import Workspace


async def _rig(
    db, *, provider: FakeProvider | None = None
) -> tuple[LoopKit, FakeProvider, Workspace]:
    ws = Workspace(db, cipher=Cipher.from_b64(generate_key_b64()))
    await ws.ensure_scaffold()
    provider = provider or FakeProvider([])  # any LLM call in a command test is a bug
    return LoopKit(provider, workspace=ws), provider, ws


@pytest.mark.integration
async def test_a_profile_line_seeds_confirmed_without_waking_the_model(db) -> None:
    kit, provider, ws = await _rig(db)
    kit.loop.submit_message(_msg("/profile I live in Lisbon"))
    await kit.loop._tick()

    assert "Noted" in kit.connector.sent[-1]
    assert [e.text for e in await ws.profile()] == ["I live in Lisbon"]
    assert kit.loop._profile_confirm is None  # nothing armed — nothing was pending
    assert provider.calls == []  # deterministic — no LLM turn
    assert kit.events.ingested == []  # and nothing became memory


@pytest.mark.integration
async def test_bare_profile_gathers_pending_into_one_batched_confirmation(db) -> None:
    kit, provider, ws = await _rig(db)
    await ws.remember("works with marine electronics", "profile", source="agent")
    await ws.remember("a morning person", "profile", source="agent")
    kit.loop.submit_message(_msg("/profile"))
    await kit.loop._tick()

    note = kit.connector.sent[-1]
    assert "Here is what I have gathered about you" in note
    assert "works with marine electronics" in note
    assert "a morning person" in note
    assert "tell me what to fix" in note
    assert kit.loop._profile_confirm is not None  # the wait is armed
    assert provider.calls == []


@pytest.mark.integration
async def test_one_yes_confirms_the_whole_batch(db) -> None:
    kit, provider, ws = await _rig(db)
    await ws.remember("works with marine electronics", "profile", source="agent")
    kit.loop.submit_message(_msg("/profile"))
    await kit.loop._tick()
    kit.loop.submit_message(_msg("yes"))
    await kit.loop._tick()

    assert "kept 1" in kit.connector.sent[-1]
    assert [e.text for e in await ws.profile()] == ["works with marine electronics"]
    assert await ws.pending_profile() == []
    assert kit.loop._profile_confirm is None  # one shot — the wait is spent
    assert provider.calls == []


@pytest.mark.integration
async def test_a_correction_is_normal_chat_and_the_guesses_stay_pending(db) -> None:
    provider = FakeProvider([Turn(text="fixed the profile")])
    kit, provider, ws = await _rig(db, provider=provider)
    await ws.remember("lives in Madrid", "profile", source="agent")
    kit.loop.submit_message(_msg("/profile"))
    await kit.loop._tick()
    kit.loop.submit_message(_msg("actually I live in Porto"))
    await kit.loop._tick()

    assert provider.calls  # the correction went to the model, not consumed
    assert [e.text for e in await ws.pending_profile()] == ["lives in Madrid"]
    assert kit.loop._profile_confirm is None  # and the wait is over


@pytest.mark.integration
async def test_a_yes_that_isnt_a_bare_yes_is_still_a_correction(db) -> None:
    provider = FakeProvider([Turn(text="just chatting now")])
    kit, provider, ws = await _rig(db, provider=provider)
    await ws.remember("a night owl", "profile", source="agent")
    kit.loop.submit_message(_msg("/profile"))
    await kit.loop._tick()
    kit.loop.submit_message(_msg("yes, but I stay up late reading"))
    await kit.loop._tick()

    # the armed wait answers a bare yes only — anything else is chat
    assert provider.calls
    assert [e.text for e in await ws.pending_profile()] == ["a night owl"]


@pytest.mark.integration
async def test_a_command_while_the_confirmation_waited_just_ends_the_wait(db) -> None:
    provider = FakeProvider([Turn(text="talked")])
    kit, provider, ws = await _rig(db, provider=provider)
    await kit.audit.append(actor="agent", tool_name="t", decision="allow")
    await ws.remember("a night owl", "profile", source="agent")
    kit.loop.submit_message(_msg("/profile"))
    await kit.loop._tick()
    kit.loop.submit_message(_msg("/verify"))
    await kit.loop._tick()
    assert "decision record" in kit.connector.sent[-1]  # the command ran
    assert kit.loop._profile_confirm is None  # and quietly ended the wait
    kit.loop.submit_message(_msg("yes"))
    await kit.loop._tick()

    # that yes arrived too late — it was ordinary chat, and the guess waits
    assert provider.calls
    assert [e.text for e in await ws.pending_profile()] == ["a night owl"]


async def test_profile_without_a_workspace_answers_honestly() -> None:
    provider = FakeProvider([])
    kit = LoopKit(provider)
    kit.loop.submit_message(_msg("/profile whatever I am"))
    await kit.loop._tick()

    assert "isn't on this instance" in kit.connector.sent[-1]
    assert provider.calls == []
