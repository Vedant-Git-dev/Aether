"""Value-based leak guard.

The verifier and the turn context carry internal values: finding ids, source
ids, bracketed tags and internal labels. An outbound message must not
contain any of them verbatim. This is a comparison against the values the
turn actually placed in context, not a phrase list and not a pattern over
model prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_BRACKET_TAG = re.compile(r"\[[^\]\n]{1,80}\]")

_INTERNAL_LABELS = (
    "[internal verification findings]",
    "finding_id",
    "source_ids",
    "internal_notes",
    "verification feedback",
    "resolutions",
)


@dataclass(frozen=True)
class LeakHit:
    value: str
    kind: str = "internal"


class LeakGuard:
    """Checks one message against the internal values collected this turn."""

    def __init__(self, values: Iterable[str]) -> None:
        self.values = tuple(sorted({value for value in values if value}))
        self._ordered = sorted(self.values, key=len, reverse=True)

    def scan(self, text: str) -> list[LeakHit]:
        if not text:
            return []
        return [LeakHit(value) for value in self._ordered if value in text]


def _tags_in(text: str) -> set[str]:
    return set(_BRACKET_TAG.findall(text or ""))


def collect_internal_values(
    *,
    findings: Iterable[Any],
    event_ids: Iterable[int],
    extra_texts: Iterable[str] = (),
) -> set[str]:
    """Every internal value placed in this turn's context.

    `findings` are the verifier's structured records; `event_ids` are the
    packed source ids the model saw; `extra_texts` are the internal prompt
    and instruction blocks (their bracketed tags are collected too).
    """
    values: set[str] = set(_INTERNAL_LABELS)
    for finding in findings:
        finding_id = str(getattr(finding, "finding_id", "") or "")
        if finding_id:
            values.add(finding_id)
            values.add(finding_id.replace("-", " "))
        for source_id in getattr(finding, "source_ids", ()) or ():
            try:
                number = int(source_id)
            except (TypeError, ValueError):
                continue
            values.add(f"[E{number}]")
            values.add(f"E{number}")
    for event_id in event_ids:
        try:
            number = int(event_id)
        except (TypeError, ValueError):
            continue
        values.add(f"[E{number}]")
        values.add(f"E{number}")
    for text in extra_texts:
        values.update(_tags_in(str(text)))
    return values


def strip_values(text: str, values: Iterable[str]) -> str:
    """Remove exact internal values from a fallback reply, longest first."""
    cleaned = text or ""
    for value in sorted((v for v in values if v), key=len, reverse=True):
        cleaned = cleaned.replace(value, "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()
