"""Carry-out honesty, unit: what the user hears after tapping Approve is
true. A tool that answers with a flattened "error: ..." string is a
failure wearing a success's shape — the old carry-out read it as
confirmation and said "Sent the email" about an email that never left.

test_agent_loop.py owns the loop's general tests; this file pins the
seam the dishonest success came from: only a clean answer reads as
done, an error answer closes the approval as failed, and what the user
hears — the model's own one reply when a reasoning provider is wired,
the plain fallback line otherwise — never claims completion.
"""

from __future__ import annotations

from fakes import FakeProvider
from test_agent_loop import LoopKit

from aether.authz.approvals import APPROVED, DENIED
from aether.llm.types import Turn


async def test_a_tool_error_answer_never_reads_as_sent() -> None:
    """The one observed lie, end to end: an approved email send whose
    provider answered with an error string. The approval closes as
    failed, the chat line says it did not go through, and the record —
    not the chat — keeps the provider's own words."""
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", "error: the account is over its sending quota")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)

    assert kit.approvals.failed == [approval.id]
    assert kit.approvals.executed == []  # it must never read as ran

    note = kit.connector.sent[-1]
    assert "didn't go through" in note
    assert "not completed" in note
    assert "quota" not in note  # the provider's words stay in the record
    assert "Sent the email" not in note  # and success is never claimed

    payload = kit.traces.created[-1]["payload"]
    assert payload["is_error"] is True
    assert "over its sending quota" in payload["result"]  # the record keeps them

    # and memory closes the loop as a failure, so a later turn never
    # remembers the act as done
    outcome = kit.events.ingested[-1]
    assert outcome["kind"] == "action_failed"
    assert outcome["payload"]["approval_id"] == approval.id


async def test_a_clean_answer_still_reads_as_sent() -> None:
    """The contrast that makes the first test mean anything: a real
    answer — even one whose words mention trouble elsewhere — closes as
    executed and says so, because it does not begin with the error mark."""
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", "message id 12345 sent")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)

    assert kit.approvals.executed == [approval.id]
    assert kit.approvals.failed == []
    assert kit.connector.sent[-1] == "Sent the email (to a@b.c)."

    outcome = kit.events.ingested[-1]
    assert outcome["kind"] == "action_done"


async def test_an_error_answer_after_a_denial_is_never_reached() -> None:
    """A denied approval never runs the tool at all — the refusal to
    narrate is not a claim about what the tool would have said."""
    kit = LoopKit(None)
    kit.add_tool("mail__send_message", "error: should never be produced")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = "denied"

    await kit.loop.execute_decision(approval.id, DENIED)

    assert kit.executed == []
    assert kit.connector.sent[-1] == "Okay — not sending an email (to a@b.c)."


async def test_an_approved_send_gets_the_model_own_reply() -> None:
    """With a reasoning provider, the acknowledgment is the model's own
    line, not a canned status: exactly the scripted reply lands, once,
    answering a prompt that carries the ask as the user read it and the
    action's own report — with no tools, so nothing further can be
    proposed."""
    provider = FakeProvider([Turn(text="Sent — it went out just now.")])
    kit = LoopKit(provider)
    kit.add_tool("mail__send_message", "message id 12345 sent")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    await kit.loop._decision_reply  # the spawned reply lands before we read

    assert kit.connector.sent == ["Sent — it went out just now."]
    assert len(provider.calls) == 1
    _, messages, tools = provider.calls[0]
    instruction = messages[-1].text
    assert "Send the email?" in instruction
    assert "To: a@b.c" in instruction
    assert "message id 12345 sent" in instruction
    assert tools == []
    assert "chat_refs" not in kit.traces.created[-1]["payload"]


async def test_a_failed_send_gets_the_model_own_reply_with_the_real_reason() -> None:
    """The failure path: the model's prompt carries the service's own error
    words, so its reply can say what went wrong — while the approval still
    closes as failed and never reads as ran."""
    provider = FakeProvider([Turn(text="It didn't go through — the account is out of quota.")])
    kit = LoopKit(provider)
    kit.add_tool("mail__send_message", "error: the account is over its sending quota")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    await kit.loop._decision_reply

    assert kit.approvals.failed == [approval.id]
    assert kit.approvals.executed == []
    assert kit.connector.sent == ["It didn't go through — the account is out of quota."]
    instruction = provider.calls[0][1][-1].text
    assert "Send the email?" in instruction
    assert "over its sending quota" in instruction  # the model sees the real error


async def test_a_denial_gets_the_model_own_acknowledgment() -> None:
    """A denial gets the model's own okay: the prompt says the user denied
    it and nothing ran, the tool never executes, and the scripted reply is
    the only thing that lands."""
    provider = FakeProvider([Turn(text="Understood — I won't send it.")])
    kit = LoopKit(provider)
    kit.add_tool("mail__send_message", "should never be produced")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = DENIED

    await kit.loop.execute_decision(approval.id, DENIED)
    await kit.loop._decision_reply

    assert kit.executed == []
    assert kit.connector.sent == ["Understood — I won't send it."]
    instruction = provider.calls[0][1][-1].text
    assert "Send the email?" in instruction
    assert "denied it — nothing ran" in instruction


async def test_a_dead_provider_still_gets_the_user_plain_words() -> None:
    """The model turn dies — the plain fallback line lands instead: a user
    who just acted never gets silence."""
    kit = LoopKit(FakeProvider([]))  # complete() raises — a dead provider
    kit.add_tool("mail__send_message", "message id 12345 sent")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    await kit.loop._decision_reply

    assert kit.connector.sent == ["Sent the email (to a@b.c)."]


async def test_an_empty_model_reply_falls_back_the_same_way() -> None:
    """A turn that comes back with nothing to say is as good as a dead one
    — the fallback line lands, never a blank message."""
    kit = LoopKit(FakeProvider([Turn(text="   ")]))
    kit.add_tool("mail__send_message", "message id 12345 sent")
    approval = await kit.approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    approval.status = APPROVED

    await kit.loop.execute_decision(approval.id, APPROVED)
    await kit.loop._decision_reply

    assert kit.connector.sent == ["Sent the email (to a@b.c)."]
