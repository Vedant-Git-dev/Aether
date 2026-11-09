"""The citation verifier: every memory claim checked against its source.

A packed chunk is only useful if the answer built on it can be checked.
This pass reads a finished reply against the chunks the turn actually
retrieved and records four kinds of claim:

  - **ids**: `[E123]` and whether event 123 was in the pack (the model
    wrote an id it never saw);
  - **quotes**: text inside quotation marks and whether it appears in a
    cited chunk — a "quote" the model composed cannot be passed off as
    the user's words;
  - **dates and numbers**: an absolute date or a long number and whether it
    appears in a chunk — the typical way a hallucinated deadline or amount
    slips in.

The verifier returns data only. It never writes user-facing text and never
appends a caveat to an answer; the composer resolves each unsupported claim
and the message catalog owns the fixed words. Keeping the two apart is what
stops internal verification language from reaching the person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_ID_RE = re.compile(r"\[({?E?)(\d+)(?:[^\]\d]*)\]")
_QUOTED_RE = re.compile(r'"([^"]{8,})"')
_DATE_RE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"  # 2026-11-03
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"  # 3/11, 03/11/26
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}"  # 3 Nov 2026
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s*\d{4}"  # Nov 3, 2026
    r")\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d{3,}\b")

MAX_QUOTE_VIOLATIONS = 6


@dataclass(frozen=True)
class Violation:
    """One claim the pack cannot support."""

    kind: str  # id | quote | date | number
    span: str
    detail: str


@dataclass(frozen=True)
class Finding:
    """One claim the verifier checked, as data.

    `finding_id` is the internal identity of this finding; `source_ids` are
    the packed events that support it (empty when nothing does). A finding
    never carries user-facing text — the composer decides what to do with
    it, and the leak guard blocks any of these values from being echoed.
    """

    finding_id: str
    kind: str  # id | quote | date | number
    claim: str
    supported: bool
    source_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "kind": self.kind,
            "claim": self.claim,
            "supported": self.supported,
            "source_ids": list(self.source_ids),
        }


def find_findings(
    reply: str,
    *,
    chunks_by_event: dict[int, list[str]],
    known_event_ids: set[int],
    extra_corpus: str = "",
) -> list[Finding]:
    """Every claim in a reply, with whether the pack supports it and from
    which source events. This is the structured verifier: it never writes
    user-facing text and never appends to the answer."""
    reply = reply or ""
    corpus = " ".join(text for texts in chunks_by_event.values() for text in texts)
    if extra_corpus:
        corpus = f"{corpus}\n{extra_corpus}"
    findings: list[Finding] = []
    counter = 0

    def add(kind: str, claim: str, supported: bool, source_ids: tuple[int, ...] = ()) -> None:
        nonlocal counter
        counter += 1
        findings.append(
            Finding(
                finding_id=f"finding-{counter}",
                kind=kind,
                claim=claim,
                supported=supported,
                source_ids=source_ids,
            )
        )

    for match in _ID_RE.finditer(reply):
        event_id = int(match.group(2))
        add("id", match.group(0), event_id in known_event_ids, (event_id,))

    for match in _QUOTED_RE.finditer(reply):
        quote = match.group(1).strip()
        if not quote:
            continue
        sources = tuple(
            sorted(
                event_id
                for event_id, texts in chunks_by_event.items()
                if _quote_supported(quote, " ".join(texts))
            )
        )
        supported = bool(sources) or (bool(extra_corpus) and _quote_supported(quote, extra_corpus))
        add("quote", match.group(0), supported, sources)
        if len(findings) >= MAX_QUOTE_VIOLATIONS:
            break

    for match in _DATE_RE.finditer(reply):
        value = match.group(0)
        sources = tuple(
            sorted(
                event_id
                for event_id, texts in chunks_by_event.items()
                if _date_supported(value, " ".join(texts))
            )
        )
        supported = bool(sources) or (bool(extra_corpus) and _date_supported(value, extra_corpus))
        add("date", value, supported, sources)

    for match in _NUMBER_RE.finditer(reply):
        value = match.group(0)
        sources = tuple(
            sorted(
                event_id for event_id, texts in chunks_by_event.items() if value in " ".join(texts)
            )
        )
        supported = bool(sources) or (bool(extra_corpus) and value in extra_corpus)
        add("number", value, supported, sources)

    return findings


def unsupported(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if not finding.supported]


def verify_reply(
    reply: str,
    *,
    chunks_by_event: dict[int, list[str]],
    known_event_ids: set[int],
    extra_corpus: str = "",
) -> list[Violation]:
    """Every unsupported claim in one reply, in a stable order.

    A thin view over `find_findings` for callers that only need the
    unsupported claims. `extra_corpus` is the conversation itself (the
    user's words and the model's earlier replies): quoting the person you
    are talking to is not a memory claim. Citation ids are checked against
    the pack alone.
    """
    details = {
        "id": "event was not in this turn's retrieved memory",
        "quote": "quoted text not found in the pack",
        "date": "date not found in the pack",
        "number": "number not found in the pack",
    }
    return [
        Violation(kind=finding.kind, span=finding.claim, detail=details[finding.kind])
        for finding in find_findings(
            reply,
            chunks_by_event=chunks_by_event,
            known_event_ids=known_event_ids,
            extra_corpus=extra_corpus,
        )
        if not finding.supported
    ]


def _quote_supported(quote: str, corpus: str) -> bool:
    normalized = _normalize(quote)
    return bool(normalized) and normalized in _normalize(corpus)


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{2,4})\b")
_MDY_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b")
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _date_supported(value: str, corpus: str) -> bool:
    """A reply date is supported when the pack holds the same calendar day,
    whatever way the two sides spell it: 2026-11-03, 3 Nov 2026 and
    Nov 3, 2026 are one date. A day/month without a year matches any year —
    the pack's own time is the only one under discussion."""
    wanted = _date_key(value)
    if wanted is None:
        return _normalize(value) in _normalize(corpus)
    return any(_keys_agree(wanted, found) for found in _corpus_date_keys(corpus))


def _date_key(value: str) -> tuple[int | None, int, int] | None:
    match = _ISO_RE.search(value)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    match = _DMY_RE.search(value)
    if match:
        month = _MONTHS.get(match.group(2).lower()[:3])
        if month:
            year = int(match.group(3))
            return (year + 2000 if year < 100 else year, month, int(match.group(1)))
    match = _MDY_RE.search(value)
    if match:
        month = _MONTHS.get(match.group(1).lower()[:3])
        if month:
            return int(match.group(3)), month, int(match.group(2))
    match = _SLASH_RE.search(value)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        if year is not None and year < 100:
            year += 2000
        return year, month, day
    return None


def _corpus_date_keys(corpus: str) -> list[tuple[int | None, int, int]]:
    keys: list[tuple[int | None, int, int]] = []
    for pattern in (_ISO_RE, _DMY_RE, _MDY_RE, _SLASH_RE):
        for match in pattern.finditer(corpus):
            key = _date_key(match.group(0))
            if key is not None:
                keys.append(key)
    return keys


def _keys_agree(wanted: tuple[int | None, int, int], found: tuple[int | None, int, int]) -> bool:
    if (wanted[1], wanted[2]) != (found[1], found[2]):
        return False
    if wanted[0] is None or found[0] is None:
        return True
    return wanted[0] == found[0]


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())
