"""Triage for free text on an unprompted turn.

A proactive turn has two possible final shapes: the model calls a terminal
outcome tool, or it writes free text. Free text is internal by definition —
but sometimes it is a complete message the person should read. This stage
asks one typed question about the candidate note: send it, or keep it
internal? The answer is model judgment with a schema, not a phrase list, and
the note itself is never rewritten, only classified.

Only a note the notification policy already allows can be triaged; when the
policy says no, the note stays internal and no model call is made.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm.base import Provider
from ..llm.types import Message, Turn, WireSpec

TRIAGE_TOOL = "decide_message"
SEND = "send"
INTERNAL = "internal"


@dataclass(frozen=True)
class TriageDecision:
    deliver: bool
    reason: str = ""


def triage_spec() -> WireSpec:
    return WireSpec(
        name=TRIAGE_TOOL,
        description=(
            "Classify the candidate note. Choose send only when the note is a "
            "complete message for the person: something they need or would act "
            "on. Choose internal when it is a decision about whether to speak, "
            "an explanation of silence, a description of your own process, a "
            "tool or check status, or reasoning about the person rather than "
            "words for them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [SEND, INTERNAL],
                    "description": "Whether the note is a message or internal.",
                },
                "reason": {
                    "type": "string",
                    "description": "One short line of reasoning. Never delivered.",
                },
            },
            "required": ["decision"],
        },
    )


def _prompt(*, observations: str, candidate: str) -> str:
    return (
        "[internal triage] Decide whether one candidate note is a message the "
        "person should read now, or internal reasoning that must stay internal. "
        "The note was written on an unprompted turn after these observations:\n"
        f"{observations or '(none)'}\n\n"
        "Candidate note:\n"
        f"{candidate}\n\n"
        "Choose send only for a complete message addressed to the person. "
        "Choose internal for a decision about whether to speak, an explanation "
        "of silence, a status of your own process, or reasoning about the "
        "person rather than words for them. Do not rewrite the note; this is a "
        "classification only."
    )


def _parse(turn: Turn) -> TriageDecision:
    for call in turn.tool_calls:
        if call.name != TRIAGE_TOOL:
            continue
        args = call.arguments if isinstance(call.arguments, dict) else {}
        decision = str(args.get("decision") or "").strip().lower()
        reason = str(args.get("reason") or "").strip()
        if decision == SEND:
            return TriageDecision(True, reason)
        if decision == INTERNAL:
            return TriageDecision(False, reason)
    return TriageDecision(False, "no decision")


async def triage_unprompted(
    provider: Provider,
    system: str,
    *,
    observations: str,
    candidate: str,
) -> TriageDecision:
    """One classification call. A failed or malformed answer keeps the note
    internal, so a broken triage can never leak words."""
    turn = await provider.complete(
        system,
        [Message.user(_prompt(observations=observations, candidate=candidate))],
        [triage_spec()],
    )
    return _parse(turn)
