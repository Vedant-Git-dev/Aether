"""Carry-out honesty, unit: what the user hears after tapping Approve is
true. A tool that answers with a flattened "error: ..." string is a
failure wearing a success's shape — the old carry-out read it as
confirmation and said "Sent the email" about an email that never left.

test_agent_loop.py owns the loop's general tests (and stays untouched);
this file pins the seam the dishonest success came from: only a clean
answer reads as done, an error answer closes the approval as failed,
says so plainly, and never claims completion.
"""

from __future__ import annotations

from test_agent_loop import LoopKit

from aether.authz.approvals import APPROVED, DENIED


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
