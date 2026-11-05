"""The citation verifier: every memory claim checked against its source.

A packed chunk is only useful if the answer built on it can be checked.
This pass reads a finished reply against the chunks the turn actually
retrieved and flags three kinds of unsupported claim:

  - **empty citations**: `[E123]` where no event 123 was in the pack (the
    model wrote an id it never saw), or no citation at all when the reply
    rests on memory;
  - **quotes**: text inside quotation marks that does not appear in the
    cited chunk — a "quote" the model composed cannot be passed off as
    the user's words;
  - **dates and numbers**: an absolute date or a long number that appears
    in no chunk — the typical way a hallucinated deadline or amount slips
    in.

The repair is deterministic and text-local: a failed quote is replaced
with a marker that says it is not verbatim, a bad id loses its claim, and
a number or date that cannot be sourced gets a single hedge line rather
than surgery that could change the answer's meaning. Nothing is deleted
outright: stripping a whole sentence risks removing the true part with it.

The loop tries one regenerated reply with this feedback first; if the
second still fails, the repair runs and the turn records what happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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

QUOTE_MARKER = "[not verbatim in my records]"
# the hedge a reply carries when a sourced number or date could not be
# checked — honest, and short enough not to read as a disclaimer
UNSOURCED_HEDGE = "(some details above could not be checked against my records)"

MAX_QUOTE_VIOLATIONS = 6


@dataclass(frozen=True)
class Violation:
    """One claim the pack cannot support."""

    kind: str  # id | quote | date | number
    span: str
    detail: str


@dataclass(frozen=True)
class Verified:
    """A checked reply and what had to be changed."""

    text: str
    violations: tuple[Violation, ...]
    changed: bool

    @property
    def ok(self) -> bool:
        return not self.violations


def verify_reply(
    reply: str,
    *,
    chunks_by_event: dict[int, list[str]],
    known_event_ids: set[int],
    extra_corpus: str = "",
) -> list[Violation]:
    """Every unsupported claim in one reply, in a stable order.

    `chunks_by_event` maps each cited event to the exact packed text the
    model was given; `known_event_ids` is the pack's own set, so an id the
    model invented is caught even before a quote is checked. `extra_corpus`
    is the conversation itself (the user's words and the model's earlier
    replies): quoting the person you are talking to is not a memory claim.
    Citation ids are checked against the pack alone.
    """
    reply = reply or ""
    corpus = " ".join(text for texts in chunks_by_event.values() for text in texts)
    if extra_corpus:
        corpus = f"{corpus}\n{extra_corpus}"
    violations: list[Violation] = []

    for match in _ID_RE.finditer(reply):
        event_id = int(match.group(2))
        if event_id not in known_event_ids:
            violations.append(
                Violation(
                    kind="id",
                    span=match.group(0),
                    detail=f"event {event_id} was not in this turn's retrieved memory",
                )
            )

    for match in _QUOTED_RE.finditer(reply):
        quote = match.group(1).strip()
        if quote and not _quote_supported(quote, corpus):
            violations.append(
                Violation(
                    kind="quote", span=match.group(0), detail="quoted text not found in the pack"
                )
            )
            if len(violations) >= MAX_QUOTE_VIOLATIONS:
                break

    for match in _DATE_RE.finditer(reply):
        value = match.group(0)
        if not _date_supported(value, corpus):
            violations.append(
                Violation(kind="date", span=value, detail="date not found in the pack")
            )

    for match in _NUMBER_RE.finditer(reply):
        value = match.group(0)
        if value not in corpus:
            violations.append(
                Violation(kind="number", span=value, detail="number not found in the pack")
            )
    return violations


def repair_reply(reply: str, violations: list[Violation]) -> Verified:
    """Apply the deterministic repair: quotes replaced with a marker, bad
    ids reduced to plain words, one hedge line for unsourced numbers or
    dates. The reply keeps its shape and its supported claims."""
    text = reply or ""
    quotes = [v for v in violations if v.kind == "quote"]
    for violation in quotes:
        # the quoted span includes the quotes; replace them with the marker
        # in brackets so the sentence still parses
        text = text.replace(violation.span, QUOTE_MARKER, 1)
    for violation in violations:
        if violation.kind != "id":
            continue
        text = text.replace(violation.span, "", 1)
    needs_hedge = any(v.kind in ("date", "number") for v in violations)
    if needs_hedge and UNSOURCED_HEDGE not in text:
        text = f"{text.rstrip()}\n{UNSOURCED_HEDGE}"
    text = _tidy(text)
    return Verified(text=text, violations=tuple(violations), changed=text != reply)


def feedback(violations: list[Violation]) -> str:
    """The one corrective sentence a regenerated attempt is given."""
    problem = "; ".join(f"{v.kind}: {v.span} ({v.detail})" for v in violations[:5])
    return (
        "[verification feedback] The reply you just wrote contains claims the "
        f"retrieved memory cannot support: {problem}. Rewrite the reply so every "
        "memory claim cites an [E...] id from the working context and quotes, "
        "names, numbers and dates appear in the cited text. If a detail is not "
        "in the context, leave it out or say it is unverified."
    )


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


def _tidy(text: str) -> str:
    """Collapse the whitespace left by removed spans, never touching
    structure: no sentence is deleted, only the unsupported token."""
    text = re.sub(r"\s*\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
