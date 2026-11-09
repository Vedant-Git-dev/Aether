"""The composer stage: findings in, one safe answer out.

The verifier never writes user-facing text. The composer receives the
structured findings, resolves every unsupported claim (`remove`, `soften`
or `ask`) and returns the words that may be delivered. It is a separate
stage from the reasoning loop, so an internal finding cannot be appended to
an answer as a caveat string.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import Provider
from ..llm.types import Message, ToolCall, ToolSpec, Turn
from ..message_catalog import render
from .leaks import strip_values

COMPOSE_TOOL = "compose_answer"
RESOLUTIONS = frozenset({"remove", "soften", "ask"})


@dataclass(frozen=True)
class Composed:
    reply: str
    internal_notes: str = ""
    resolutions: dict[str, str] = field(default_factory=dict)


def compose_spec() -> ToolSpec:
    return ToolSpec(
        name=COMPOSE_TOOL,
        description=(
            "Return the final answer. For every unsupported finding, choose one "
            "resolution: remove the claim, soften it in your own words, or ask the "
            "user. Never put finding ids, source ids, bracketed tags or internal "
            "labels in `reply`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "The complete answer the user reads.",
                },
                "internal_notes": {
                    "type": "string",
                    "description": "Why you resolved the findings this way. Never delivered.",
                },
                "resolutions": {
                    "type": "array",
                    "description": "One entry for each unsupported finding.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": sorted(RESOLUTIONS),
                            },
                        },
                        "required": ["finding_id", "action"],
                    },
                },
            },
            "required": ["reply"],
        },
    )


def _prompt(
    findings: list[Any],
    candidate: str,
    *,
    safe_only: bool,
) -> str:
    if safe_only:
        return (
            "[safe composer] Rewrite the answer using only the person's own words "
            "above. Do not mention finding ids, source ids, bracketed tags, "
            "verification, or internal labels. If a detail is not available in the "
            "safe view, leave it out. Call compose_answer with the reply."
        )
    payload = [
        finding.as_dict() if hasattr(finding, "as_dict") else dict(finding) for finding in findings
    ]
    return (
        "[internal verification findings] These are data for you, never words to "
        "quote. Resolve every finding whose supported flag is false: remove the "
        "claim, soften it in your own words, or ask the user. Do not put any "
        "finding_id, source id, bracketed tag, or internal label in reply. Put "
        "your reasons in internal_notes.\n\n"
        f"Candidate answer:\n{candidate}\n\n"
        f"Findings:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse(turn: Turn) -> Composed | None:
    for call in turn.tool_calls:
        if call.name != COMPOSE_TOOL:
            continue
        args = call.arguments if isinstance(call.arguments, dict) else {}
        reply = str(args.get("reply") or "").strip()
        notes = str(args.get("internal_notes") or "").strip()
        resolutions: dict[str, str] = {}
        raw = args.get("resolutions")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                finding_id = str(item.get("finding_id") or "").strip()
                action = str(item.get("action") or "").strip().lower()
                if finding_id and action in RESOLUTIONS:
                    resolutions[finding_id] = action
        return Composed(reply=reply, internal_notes=notes, resolutions=resolutions)
    return None


async def compose_answer(
    provider: Provider,
    system: str,
    history: list[Message],
    *,
    findings: list[Any],
    candidate: str,
    safe_only: bool = False,
) -> Composed | None:
    """One composer call. Returns None when the model did not use the
    structured tool, so the caller can apply its deterministic fallback."""
    turn = await provider.complete(
        system,
        [*history, Message.user(_prompt(findings, candidate, safe_only=safe_only))],
        [compose_spec()],
    )
    return _parse(turn)


def unresolved(findings: list[Any], resolutions: dict[str, str]) -> list[Any]:
    """The unsupported findings the composer did not resolve."""
    missing = []
    for finding in findings:
        if getattr(finding, "supported", True):
            continue
        if str(getattr(finding, "finding_id", "")) not in resolutions:
            missing.append(finding)
    return missing


def deterministic_compose(
    reply: str,
    findings: list[Any],
    *,
    locale: str = "en",
    internal_values: Iterable[str] = (),
) -> str:
    """The fallback when the composer does not answer.

    Every unsupported claim is removed by exact value, every internal id and
    tag is stripped, and the fixed caveat comes from the message catalog —
    the verifier never supplies user-facing words of its own.
    """
    text = reply or ""
    for finding in findings:
        claim = str(getattr(finding, "claim", "") or "")
        if claim:
            text = text.replace(claim, "", 1)
    text = strip_values(text, internal_values)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    if findings:
        if not text:
            return render("verification.unsupported", locale=locale)
        caveat = render("verification.caveat", locale=locale, count=len(findings))
        return f"{text}\n{caveat}".strip()
    return text or render("verification.safe_fallback", locale=locale)


def tool_call_for_testing(
    reply: str,
    *,
    internal_notes: str = "",
    resolutions: dict[str, str] | None = None,
) -> Turn:
    """The structured turn tests script — same shape the composer parses."""
    return Turn(
        tool_calls=[
            ToolCall(
                id="compose",
                name=COMPOSE_TOOL,
                arguments={
                    "reply": reply,
                    "internal_notes": internal_notes,
                    "resolutions": [
                        {"finding_id": key, "action": value}
                        for key, value in (resolutions or {}).items()
                    ],
                },
            )
        ]
    )
