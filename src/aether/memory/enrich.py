"""Write-time enrichment: understand an event once, so questions are lookups.

Two stages, in this order and for a reason:

1. **The heuristic floor** is deterministic and runs inside the ingest path.
   Words like "ASAP", "urgent", "within 2 hours", "by EOD" or "deadline"
   raise urgency on their own, so an urgent mail is never invisible while a
   model call is queued, rate-limited or absent. It costs nothing and it is
   the reason the pipeline cannot silently fail open.

2. **The classifier** is one cheap model turn returning strict JSON —
   urgency, needs_reply, action_type, deadline phrases, commitments and a
   one-line summary. Its failure is not an error state: the event keeps the
   floor's urgency and stays unscored rather than unscorable.

Time normalisation sits between them and is deliberately *not* the model's
job. The model hands back the phrase it saw ("by end of day", "within 48
hours"); `normalize_deadline` turns that into an absolute instant against
the event's own timestamp and the user's timezone, and keeps the original
words. A relative deadline that never became absolute is the bug this whole
section exists to fix, so it is parsed deterministically and unit-tested
rather than asked of a model that cannot know the event's timestamp.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..llm.json_utils import extract_json
from ..llm.types import Message
from .mail import MAIL_KIND

log = logging.getLogger("aether.memory.enrich")

MAX_URGENCY = 3
FLOOR_URGENCY = 2
SUMMARY_CHARS = 140

# The deterministic floor's vocabulary. Deliberately short and
# literal: a false positive costs one wasted look, a false negative hides
# an urgent message.
FLOOR_TERMS = (
    "asap",
    "urgent",
    "urgently",
    "immediately",
    "right away",
    "as soon as possible",
    "by eod",
    "end of day",
    "end of business",
    "close of business",
    "by cob",
    "deadline",
    "due today",
    "due tomorrow",
    "time-sensitive",
    "time sensitive",
    "action required",
    "final notice",
    "last chance",
)

_WITHIN = re.compile(
    r"\b(?:within|in)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b",
    re.IGNORECASE,
)
_HOURS = re.compile(r"\b(\d+)\s*(?:more\s+)?(hours?|hrs?)\b", re.IGNORECASE)
_NAMED_DAY = re.compile(
    r"\b(?:by|on|before|due)?\s*"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DATED_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%b %d %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%d %B %Y",
)
# a date with no year: the year is spliced in, never left to strptime's
# ambiguous day-of-month default
_YEARLESS_FORMATS = ("%b %d", "%d %b", "%B %d", "%d %B")

# an "end of day" deadline means the end of the working day, not midnight —
# a reminder that fires after the recipient has gone home is not a reminder
EOD_HOUR = 17
NIGHT_HOUR = 21
MORNING_HOUR = 9


class EnricherError(RuntimeError):
    """The classifier could not produce a usable judgment."""


def heuristic_floor(text: str) -> int:
    """Urgency a deterministic scan alone can justify: 0, or the floor.

    Runs before any model call and inside the ingest path, so it is what
    keeps "anything urgent?" answerable when the classifier is late or
    switched off.
    """
    lowered = (text or "").lower()
    if any(term in lowered for term in FLOOR_TERMS):
        return FLOOR_URGENCY
    if _WITHIN.search(lowered) or _HOURS.search(lowered):
        # "within 2 hours", "in 3 days", "48 hours" — a stated window is a
        # deadline whether or not the word "deadline" appears
        return FLOOR_URGENCY
    return 0


@dataclass(frozen=True)
class Deadline:
    """One resolved deadline: the absolute instant, the words it came from,
    and the end of the window when the phrase named a range."""

    at: datetime
    phrase: str
    end: datetime | None = None

    def is_past(self, now: datetime) -> bool:
        return (self.end or self.at) < now


@dataclass(frozen=True)
class ExtractedCommitment:
    """A promise or request the event carries, before it is stored."""

    kind: str  # task | request | promise | awaiting_reply
    text: str
    due_at: datetime | None = None


@dataclass(frozen=True)
class ExtractedFact:
    """One durable claim the event carries, before it is consolidated.

    The subject is the name or handle the claim is about as the model read
    it; the caller resolves it to an identity where one exists. The value is
    copied, never paraphrased — it is what a later question has to be able to
    match and what the core-facts block quotes.
    """

    subject: str
    key: str
    value: str
    type: str = "fact"


@dataclass
class Enrichment:
    """What one event means. Every field has a safe default, so a partial
    or failed classification degrades to "unknown" and never to a guess."""

    urgency: int = 0
    needs_reply: bool = False
    action_type: str = ""
    summary: str = ""
    noise: bool = False
    deadlines: list[Deadline] = field(default_factory=list)
    commitments: list[ExtractedCommitment] = field(default_factory=list)
    facts: list[ExtractedFact] = field(default_factory=list)
    classified: bool = False

    @property
    def deadline_at(self) -> datetime | None:
        """The earliest deadline — what "due" means when there are several."""
        if not self.deadlines:
            return None
        return min(deadline.at for deadline in self.deadlines)

    @property
    def deadline_end(self) -> datetime | None:
        if not self.deadlines:
            return None
        return max((deadline.end or deadline.at) for deadline in self.deadlines)

    @property
    def deadline_phrase(self) -> str:
        if not self.deadlines:
            return ""
        earliest = min(self.deadlines, key=lambda deadline: deadline.at)
        return earliest.phrase

    def to_extra(self) -> dict[str, Any]:
        """The sealed-meta fields this reading contributes.

        Only what was actually learned is written, so an unclassified event
        keeps its floor urgency and carries no empty summary pretending to
        be one.
        """
        extra: dict[str, Any] = {"urgency": self.urgency}
        if self.needs_reply:
            extra["needs_reply"] = True
        if self.noise:
            extra["noise"] = True
        if self.action_type:
            extra["action_type"] = self.action_type
        if self.summary:
            extra["summary"] = self.summary
        earliest = self.deadline_at
        if earliest is not None:
            extra["deadline_at"] = earliest.isoformat()
            extra["deadline_phrase"] = self.deadline_phrase
            end = self.deadline_end
            if end is not None and end != earliest:
                extra["deadline_end"] = end.isoformat()
        if self.classified:
            extra["classified"] = True
        return extra


def enrichment_text(kind: str, payload: dict[str, Any]) -> str:
    """The text an event is understood from — its words, not its JSON.

    A mail reads as its subject, sender, snippet and body; a chat message
    is what was said; a document is its name, caption and text. Anything
    else falls back to the payload's own rendering, which is what the
    salience judge has always seen.
    """
    data = payload or {}
    if kind == MAIL_KIND:
        parts = (
            data.get("subject"),
            data.get("sender"),
            data.get("snippet"),
            data.get("body"),
        )
        return "\n".join(str(part) for part in parts if part)
    if kind == "chat_message":
        return str(data.get("text") or "")
    if kind in _DOCUMENT_KINDS:
        parts = (data.get("filename"), data.get("caption"), data.get("text"))
        return "\n".join(str(part) for part in parts if part)
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


_DOCUMENT_KINDS = frozenset({"shared_doc", "shared_image", "shared_audio", "shared_video"})


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r — falling back to UTC", tz)
        return ZoneInfo("UTC")


def _local(now: datetime, zone: ZoneInfo) -> datetime:
    return (
        now.astimezone(zone) if now.tzinfo is not None else now.replace(tzinfo=UTC).astimezone(zone)
    )


def _unit_delta(count: int, unit: str) -> timedelta | None:
    unit = unit.lower()
    if unit.startswith("min"):
        return timedelta(minutes=count)
    if unit.startswith(("hour", "hr")):
        return timedelta(hours=count)
    if unit.startswith("day"):
        return timedelta(days=count)
    if unit.startswith("week"):
        return timedelta(weeks=count)
    if unit.startswith("month"):
        return timedelta(days=30 * count)  # a month is a window, not a date
    return None


def normalize_deadline(
    phrase: str,
    *,
    occurred_at: datetime,
    timezone: str = "Asia/Kolkata",
) -> Deadline | None:
    """One deadline phrase -> an absolute instant, deterministically.

    Resolved against the event's own timestamp, in the user's timezone, so
    "by 6pm" in a mail that arrived on Tuesday is Tuesday 6pm and not the
    moment the poll happened to run. The original words travel with the
    result; a phrase this cannot read returns None and is left for the
    classifier's own reading rather than being guessed at.

    A date without a clock ("by Nov 5") ends at the working day's close,
    and "next week" resolves to a range, because that is what the phrase
    actually means.
    """
    text = (phrase or "").strip()
    if not text:
        return None
    zone = _zone(timezone)
    local = _local(occurred_at, zone)
    lowered = text.lower()
    clock = _clock_time(lowered)

    within = _WITHIN.search(lowered)
    if within:
        delta = _unit_delta(int(within.group(1)), within.group(2))
        if delta is not None:
            return Deadline(at=(occurred_at + delta).astimezone(UTC), phrase=text)

    if "next week" in lowered:
        start, end = _next_week(local)
        return Deadline(at=start.astimezone(UTC), phrase=text, end=end.astimezone(UTC))

    if "tomorrow" in lowered:
        return Deadline(at=_day_at(local + timedelta(days=1), clock).astimezone(UTC), phrase=text)

    if "tonight" in lowered:
        # never rolled forward: an expired "tonight" should read as overdue,
        # not silently become tomorrow night
        return Deadline(at=_day_at(local, clock or (NIGHT_HOUR, 0)).astimezone(UTC), phrase=text)

    if re.search(r"\btoday\b", lowered) or "end of day" in lowered or "by eod" in lowered:
        return Deadline(at=_day_at(local, clock, allow_next_day=True).astimezone(UTC), phrase=text)

    named = _NAMED_DAY.search(lowered)
    if named:
        day = _next_weekday(local, _WEEKDAY_INDEX[named.group(1).lower()])
        return Deadline(at=_day_at(day, clock).astimezone(UTC), phrase=text)

    absolute = _absolute_date(text, local, clock)
    if absolute is not None:
        return Deadline(at=absolute.astimezone(UTC), phrase=text)

    if clock is not None:
        return Deadline(at=_day_at(local, clock, allow_next_day=True).astimezone(UTC), phrase=text)

    return None


# "by 6pm", "at 18:30", "5 pm" — a clock the phrase named
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_CLOCK_24 = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_CLOCK_STRIP = re.compile(
    r"\b(?:at|by|before|until|till)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
_LEADING_PREPOSITION = re.compile(r"^(?:due\s+)?(?:by|on|before|until|till|at)\s+", re.IGNORECASE)


def _clock_time(text: str) -> tuple[int, int] | None:
    """The time of day a phrase named, 24-hour, or None when it named none."""
    match = _CLOCK.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if match.group(3).lower() == "pm" and hour < 12:
            hour += 12
        elif match.group(3).lower() == "am" and hour == 12:
            hour = 0
        return (hour, minute) if hour <= 23 and minute <= 59 else None
    match = _CLOCK_24.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return (hour, minute) if hour <= 23 and minute <= 59 else None
    return None


def _day_at(
    day: datetime,
    clock: tuple[int, int] | None,
    *,
    allow_next_day: bool = False,
) -> datetime:
    """One day plus the clock the phrase gave, defaulting to the working
    day's close. `allow_next_day` is for a bare "by EOD"/"by 5pm" said after
    that hour has passed: it means the next one."""
    hour, minute = clock if clock is not None else (EOD_HOUR, 0)
    target = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if allow_next_day and target <= day:
        target += timedelta(days=1)
    return target


def _next_weekday(local: datetime, weekday: int) -> datetime:
    """The next occurrence of a weekday, never today — "by Friday" said on
    a Friday means the Friday coming, not the one already going."""
    ahead = (weekday - local.weekday()) % 7 or 7
    return (local + timedelta(days=ahead)).replace(hour=0, minute=0, second=0, microsecond=0)


def _next_week(local: datetime) -> tuple[datetime, datetime]:
    ahead = 7 - local.weekday()  # the coming Monday
    monday = (local + timedelta(days=ahead)).replace(
        hour=MORNING_HOUR, minute=0, second=0, microsecond=0
    )
    friday = monday + timedelta(days=4)
    return monday, friday.replace(hour=EOD_HOUR)


def _absolute_date(text: str, local: datetime, clock: tuple[int, int] | None) -> datetime | None:
    """A written date, with or without a clock.

    A written year is taken as written. A year-less form ("Nov 5") takes
    this year and rolls forward when that date has already gone by, so a
    phrase about a date that has passed means the next one, not the past.
    The year is spliced into the string rather than left to strptime's
    year default, which is ambiguous for a day-of-month and is going away.
    """
    cleaned = _LEADING_PREPOSITION.sub("", _CLOCK_STRIP.sub("", text)).strip().strip(".,;:")
    if not cleaned:
        return None
    hour, minute = clock if clock is not None else (EOD_HOUR, 0)

    for fmt in _DATED_FORMATS:
        candidate = _parse_date(cleaned, fmt, local, hour, minute)
        if candidate is not None:
            return candidate

    for fmt in _YEARLESS_FORMATS:
        candidate = _parse_date(f"{cleaned} {local.year}", f"{fmt} %Y", local, hour, minute)
        if candidate is None:
            continue
        if candidate.date() < local.date():
            candidate = candidate.replace(year=local.year + 1)
        return candidate
    return None


def _parse_date(text: str, fmt: str, local: datetime, hour: int, minute: int) -> datetime | None:
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError:
        return None
    return parsed.replace(hour=hour, minute=minute, tzinfo=local.tzinfo)


class Classifier(Protocol):
    async def classify(self, text: str) -> dict[str, Any]:
        """The model's strict-JSON reading of one event."""


class LLMClassifier:
    """Adapts any `llm.Provider` into the enrichment classifier: exactly one
    short turn, strict JSON, no tools."""

    PROMPT = (
        "You read one event from a personal assistant's memory and answer in "
        "strict JSON only.\n"
        "Keys:\n"
        '  "urgency": 0 (noise) to 3 (act now)\n'
        '  "needs_reply": true when the user is expected to answer\n'
        '  "action_type": one short word, e.g. "reply", "pay", "submit", "none"\n'
        '  "deadlines": list of deadline phrases copied verbatim from the text\n'
        '  "commitments": list of {"kind": "task"|"request"|"promise"|'
        '"awaiting_reply", "text": <short description>, "due": <phrase or "">}\n'
        '  "facts": list of {"subject": <who it is about, or "user">, "key": '
        '<short label such as "timezone" or "preferred_meeting_time">, '
        '"value": <the claim copied verbatim>, "type": "fact"|"preference"|'
        '"rule"}\n'
        '  "summary": one line, at most 140 characters\n'
        '  "noise": true for newsletters, receipts, alerts and automated mail\n'
        "Copy deadline phrases exactly as written — do not convert them to "
        "dates. Reply with the JSON object and nothing else."
    )

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def classify(self, text: str) -> dict[str, Any]:
        if self._provider is None:
            raise EnricherError("no LLM provider configured")
        turn = await self._provider.complete(self.PROMPT, [Message.user(text)], tools=[])
        try:
            data = extract_json(turn.text)
        except Exception as exc:
            # one contract for "this reading is unusable", whatever shape the
            # model answered in — the enricher only has to know the one
            raise EnricherError(f"the classifier returned no JSON object: {exc}") from exc
        if not isinstance(data, dict):
            raise EnricherError("the classifier returned no JSON object")
        return data


class Enricher:
    """Turns one event's text into an `Enrichment`.

    The floor is applied first and always; the classifier is best effort.
    A classifier that fails leaves `classified` False and the event keeps
    whatever the floor justified — never nothing.
    """

    def __init__(
        self,
        classifier: Classifier,
        *,
        timezone: str = "Asia/Kolkata",
        summary_chars: int = SUMMARY_CHARS,
    ) -> None:
        self._classifier = classifier
        self._timezone = timezone
        self._summary_chars = summary_chars

    async def enrich(self, text: str, *, occurred_at: datetime) -> Enrichment:
        result = Enrichment(urgency=heuristic_floor(text))
        try:
            data = await self._classifier.classify(text)
        except Exception:
            log.warning("enrichment classifier failed — keeping the heuristic floor")
            return result

        result.classified = True
        result.urgency = max(result.urgency, _clamp_urgency(data.get("urgency")))
        result.needs_reply = bool(data.get("needs_reply", False))
        result.action_type = str(data.get("action_type") or "").strip()[:40]
        result.noise = bool(data.get("noise", False))
        result.summary = str(data.get("summary") or "").strip()[: self._summary_chars]
        result.deadlines = self._deadlines(data.get("deadlines"), occurred_at)
        result.commitments = self._commitments(data.get("commitments"), occurred_at)
        result.facts = self._facts(data.get("facts"))
        return result

    def _deadlines(self, raw: Any, occurred_at: datetime) -> list[Deadline]:
        if not isinstance(raw, list):
            return []
        found: list[Deadline] = []
        for phrase in raw:
            if not isinstance(phrase, str):
                continue
            deadline = normalize_deadline(phrase, occurred_at=occurred_at, timezone=self._timezone)
            if deadline is not None:
                found.append(deadline)
        # the classifier may echo the same window twice; one is enough
        unique: dict[datetime, Deadline] = {}
        for deadline in found:
            unique.setdefault(deadline.at, deadline)
        return sorted(unique.values(), key=lambda deadline: deadline.at)

    def _commitments(self, raw: Any, occurred_at: datetime) -> list[ExtractedCommitment]:
        if not isinstance(raw, list):
            return []
        out: list[ExtractedCommitment] = []
        for item in raw:
            if isinstance(item, str):
                text, kind, due = item, "task", ""
            elif isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                kind = str(item.get("kind") or "task").strip().lower()
                due = str(item.get("due") or "").strip()
            else:
                continue
            if not text:
                continue
            deadline = normalize_deadline(due, occurred_at=occurred_at, timezone=self._timezone)
            out.append(
                ExtractedCommitment(
                    kind=kind if kind in _COMMITMENT_KINDS else "task",
                    text=text[:200],
                    due_at=deadline.at if deadline else None,
                )
            )
        return out

    def _facts(self, raw: Any) -> list[ExtractedFact]:
        """Candidate claims the event carries, coerced and bounded.

        A claim with no subject, key or value is dropped rather than stored
        as an unnamed belief; the type falls back to "fact" when the model
        named something unknown.
        """
        if not isinstance(raw, list):
            return []
        out: list[ExtractedFact] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()[:_FACT_SUBJECT_CAP]
            key = str(item.get("key") or "").strip()[:_FACT_KEY_CAP]
            value = str(item.get("value") or "").strip()[:_FACT_VALUE_CAP]
            if not subject or not key or not value:
                continue
            fact_type = str(item.get("type") or "fact").strip().lower()
            out.append(
                ExtractedFact(
                    subject=subject,
                    key=key,
                    value=value,
                    type=fact_type if fact_type in _FACT_TYPES else "fact",
                )
            )
        return out


_COMMITMENT_KINDS = frozenset({"task", "request", "promise", "awaiting_reply"})

_FACT_TYPES = frozenset({"fact", "preference", "rule", "identity", "contact", "policy"})
_FACT_SUBJECT_CAP = 80
_FACT_KEY_CAP = 80
_FACT_VALUE_CAP = 400


def _clamp_urgency(value: Any) -> int:
    try:
        return max(0, min(MAX_URGENCY, int(value)))
    except (TypeError, ValueError):
        return 0
