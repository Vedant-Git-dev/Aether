"""The query router: what a question is about, before anything is searched.

Three deterministic steps run before retrieval and cheaply beat it:

  1. **Reference resolution.** "send it", "send then", "the sender",
     "the assignment" resolve from the focus object — the last offer and
     its options, the open task, the last attachment — to a concrete id.
     An ambiguous reference asks ONE specific question with the real
     options instead of searching and hoping. Anything unresolved falls
     through to a normal search.

  2. **Classification and routing.** Rules first: the attention keywords,
     commitment words, a name the focus already knows, "slide 3"/"page 2",
     fact questions, greetings. Each route carries the filters retrieval
     needs (`{time_range, entities, source, doc_ref, urgency_min}`). The
     default is open recall. (A small model fallback for questions the
     rules cannot place is a later flag: the rules own the route until the
     evaluation shows the model improves it.)

  3. **Rewrite.** A short or anaphoric question ("what about that?") is
     expanded with the focus object's people, open task and attachment
     names into up to three keyword queries plus the exact tokens the user
     typed — cached by normalized message and focus hash, because the same
     question in the same conversation deserves the same rewrite.

This module is pure and synchronous: it reads a `FocusState`, emits a
`Routing`, and never touches the network, the database or the clock beyond
the `now` it is handed. Everything user-derived stays in process memory.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .focus import FocusState
from .views import wants_attention

_CHIT_CHAT = frozenset(
    [
        "hi",
        "hello",
        "hey",
        "yo",
        "thanks",
        "thank",
        "you",
        "okay",
        "ok",
        "cool",
        "great",
        "nice",
        "good",
        "morning",
        "evening",
        "afternoon",
        "night",
        "bye",
        "goodbye",
        "hmm",
        "huh",
        "yes",
        "no",
        "sure",
        "there",
        "how",
        "are",
        "up",
        "doing",
        "so",
        "much",
        "the",
        "for",
        "help",
        "again",
        "please",
    ]
)

_URLS_OR_EXT_RE = re.compile(r"\b([\w.\-]+\.(?:pdf|docx|doc|pptx|ppt|xlsx|xls|csv|txt|md))\b", re.I)
_SLIDE_RE = re.compile(r"\b(slide|page)\s+(\d+)\b", re.I)
_SEND_TARGET_RE = re.compile(
    r"\b(send|share|forward|deliver|attach|reply\s+with|email)\s+"
    r"(it|that|this|them|those|these|then)\b",
    re.I,
)
_SAME_PERSON_RE = re.compile(
    r"\b(same\s+person|the\s+sender|that\s+sender|same\s+sender|"
    r"who\s+emailed\s+me|who\s+wrote|who\s+sent)\b",
    re.I,
)
_TASK_NOUN_RE = re.compile(
    r"\b(?:the\s+)?(assignment|file|document|pdf|deck|report|slides|upload|spreadsheet)\b",
    re.I,
)
_LIVE_NOW_RE = re.compile(
    r"\b(anything\s+new|what'?s\s+new|any\s+updates?|latest|right\s+now|"
    r"inbox\s+right\s+now|as\s+of\s+now)\b",
    re.I,
)
_COMMITMENT_RE = re.compile(
    r"\b(promise|promised|commit|committed|remind|reminder|"
    r"what\s+did\s+i\s+(promise|agree|commit)|my\s+tasks?|todo|to-do)\b",
    re.I,
)
_FACT_RE = re.compile(
    r"\b(what'?s\s+my|what\s+is\s+my|how\s+do\s+i\s+(like|prefer|want)|"
    r"do\s+i\s+(like|prefer)|my\s+(favourite|favorite|preference|preferred|"
    r"address|email|phone|number|birthday|dob|name|timezone))\b",
    re.I,
)
_MAIL_RE = re.compile(r"\b(email|emails|mail|mails|inbox|gmail)\b", re.I)
_TIME_TODAY_RE = re.compile(r"\b(today|this morning|tonight)\b", re.I)
_TIME_YESTERDAY_RE = re.compile(r"\b(yesterday|last night)\b", re.I)
_TIME_WEEK_RE = re.compile(r"\b(this week|last week|past week)\b", re.I)

# the anaphora that says "this question is not carrying its own content"
_ANAPHORA = frozenset(
    ["it", "that", "this", "them", "those", "these", "then", "there", "same", "one", "those"]
)


@dataclass(frozen=True)
class RouteFilters:
    """What the route narrowed: the retrievers apply these, never SQL."""

    entity_ids: tuple[int, ...] = ()
    source: str = ""
    doc_ref: str = ""
    location: str = ""
    time_from: datetime | None = None
    time_until: datetime | None = None
    urgency_min: int = 0


@dataclass(frozen=True)
class Resolved:
    """A reference that resolved to a direct id — retrieval is skipped."""

    kind: str  # attachment | entity | task
    label: str
    event_id: int | None = None
    media_id: int | None = None
    entity_id: int | None = None


@dataclass(frozen=True)
class Routing:
    """One question, classified and prepared for retrieval."""

    text: str
    routes: tuple[str, ...]
    filters: RouteFilters = field(default_factory=RouteFilters)
    resolved: Resolved | None = None
    ask: str | None = None
    queries: tuple[str, ...] = ()
    exact_tokens: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def needs_search(self) -> bool:
        return bool(self.routes) and "reference" not in self.routes and "chat" not in self.routes


# -- reference resolution ----------------------------------------------------


def resolve_reference(
    text: str, focus: FocusState | None, *, entity_names: dict[int, str] | None = None
) -> tuple[Resolved | None, str | None, str | None]:
    """Resolve "it", "then", "the sender", "the assignment" from the focus.

    Returns `(resolved, ask, entity_name_hint)`:
    - resolved: the concrete id the question points at (no search needed);
    - ask: ONE specific question when the object does not carry enough to
      pick a target — named options, never a generic "which one?";
    - entity_name_hint: the person's display name when "the sender"
      resolved, so the caller can still show who was meant.
    """
    text = (text or "").strip()
    if not text or focus is None:
        return None, None, None
    names = entity_names or {}

    if _SAME_PERSON_RE.search(text):
        if focus.recent_entities:
            entity_id = focus.recent_entities[0]
            label = names.get(entity_id, "the person from your last conversation")
            return (
                Resolved(kind="entity", label=label, entity_id=entity_id),
                None,
                label,
            )
        return None, None, None

    task_noun = _TASK_NOUN_RE.search(text)
    # a bare "Assignment?" or a send verb makes the noun a reference; an
    # ordinary sentence ("explain the report") is a search, not a target
    is_reference = bool(
        _SEND_TARGET_RE.search(text)
        or (
            task_noun
            and (
                text.strip().endswith("?")
                or len(text.split()) <= 2
                or re.match(r"^(send|share|forward|deliver|attach|email)", text, re.I)
            )
        )
    )
    if is_reference:
        target = _select_target(focus, task_noun.group(0) if task_noun else "")
        if target is not None:
            return target, None, None
        if not _SEND_TARGET_RE.search(text):
            # an information question about the noun: search it, don't ask
            return None, None, None
        # the reference is live but the object has no candidate: ask one
        # specific question rather than searching for a pronoun
        options = _offer_options(focus)
        if len(options) > 1:
            return None, f"Which one — {options[0]} or {options[1]}?", None
        return None, "What should I send?", None
    return None, None, None


def _select_target(focus: FocusState, noun: str) -> Resolved | None:
    """The one plausible target, or None when nothing fits."""
    attachment = focus.last_attachment or {}
    task = focus.open_task or {}
    offer = focus.last_offer or {}

    # "the assignment" matches a task whose summary says so
    if noun:
        topic = noun.split()[-1].lower()
        summary = str(task.get("summary") or "")
        if task and topic in summary.lower():
            return Resolved(
                kind="task", label=summary[:200], event_id=_as_int(task.get("event_id"))
            )
        if attachment and topic in str(attachment.get("filename") or "").lower():
            return _attachment_resolution(attachment)
        if offer and topic in str(offer.get("text") or "").lower():
            return None  # the offer mentions it; the search will find it
    if attachment:
        return _attachment_resolution(attachment)
    if task:
        return Resolved(
            kind="task",
            label=str(task.get("summary") or "the open task")[:200],
            event_id=_as_int(task.get("event_id")),
        )
    return None


def _attachment_resolution(attachment: dict) -> Resolved:
    return Resolved(
        kind="attachment",
        label=str(attachment.get("filename") or "the last attachment")[:200],
        event_id=_as_int(attachment.get("event_id")),
        media_id=_as_int(attachment.get("media_id")),
    )


def _offer_options(focus: FocusState) -> list[str]:
    offer = focus.last_offer or {}
    options = offer.get("options")
    if isinstance(options, list):
        return [str(option) for option in options if str(option).strip()]
    return []


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# -- classification and route -------------------------------------------------


def classify(
    text: str,
    *,
    focus: FocusState | None = None,
    entity_names: dict[int, str] | None = None,
    now: datetime | None = None,
) -> Routing:
    """Rules first: turn one question into routes and filters.

    The order matters — a resolved reference wins, then attention, then
    state-of-now, then the specific routes, and open recall is the floor.
    A greeting gets no route at all: nothing is retrieved for "hi".
    """
    text = (text or "").strip()
    now = now or datetime.now(UTC)
    if not text:
        return Routing(text=text, routes=())

    if _is_chit_chat(text):
        return Routing(text=text, routes=("chat",))

    resolved, ask, name_hint = resolve_reference(text, focus, entity_names=entity_names)
    notes: list[str] = []
    filters = _base_filters(text, entity_names=entity_names, now=now)
    routes: list[str] = []

    if resolved is not None:
        if resolved.entity_id is not None:
            filters = _with_entities(filters, (resolved.entity_id,))
        return Routing(
            text=text,
            routes=("reference",),
            filters=filters,
            resolved=resolved,
            ask=ask,
            notes=tuple(notes),
        )

    if ask is not None:
        # a reference the focus cannot settle: ask before searching, but
        # still carry the open-recall route so a stray topic word can help
        routes.append("reference")
        if not _only_anaphora(text):
            routes.append("open_recall")

    if wants_attention(text):
        routes.append("attention")
        filters = _with_urgency(filters, 1)
    if _LIVE_NOW_RE.search(text):
        routes.append("live_now")
    if _COMMITMENT_RE.search(text):
        routes.append("commitment")
    if _SLIDE_RE.search(text):
        routes.append("doc")

    named = _named_entity_ids(text, entity_names or {})
    if named:
        routes.append("person")
        filters = _with_entities(filters, named)
    elif name_hint and _SAME_PERSON_RE.search(text):
        routes.append("person")

    if _FACT_RE.search(text):
        routes.append("fact")
        notes.append("facts layer is not built yet — falling back to open recall")

    if not routes:
        routes.append("open_recall")
    elif "open_recall" not in routes and _has_topic_words(text):
        # "anything urgent about the invoice?" still needs the topic search
        routes.append("open_recall")

    return Routing(
        text=text,
        routes=tuple(_dedupe(routes)),
        filters=filters,
        resolved=resolved,
        ask=ask,
        notes=tuple(notes),
    )


def _is_chit_chat(text: str) -> bool:
    words = [word.strip("!?.,").lower() for word in text.split()]
    return bool(words) and len(words) <= 4 and all(word in _CHIT_CHAT for word in words if word)


def _only_anaphora(text: str) -> bool:
    words = {word.strip("!?.,").lower() for word in text.split()}
    return words <= _ANAPHORA | {"send", "share", "forward", "deliver", "attach", "reply", "with"}


def _has_topic_words(text: str) -> bool:
    """Beyond the trigger words, is there anything to search for?"""
    words = [word.strip("!?.,").lower() for word in text.split()]
    triggers = {
        "urgent",
        "asap",
        "pending",
        "deadline",
        "overdue",
        "attention",
        "due",
        "today",
        "tomorrow",
        "needs",
        "reply",
        "waiting",
        "follow",
        "up",
        "action",
        "required",
        "anything",
        "what",
        "what's",
        "whats",
        "is",
        "there",
        "new",
        "latest",
        "now",
        "right",
        "kuch",
        "koi",
        "hai",
    }
    return any(word and word not in triggers and len(word) >= 3 for word in words)


def _named_entity_ids(text: str, entity_names: dict[int, str]) -> tuple[int, ...]:
    found: list[int] = []
    lowered = text.lower()
    for entity_id, name in entity_names.items():
        clean = (name or "").strip().lower()
        if len(clean) < 3:
            continue
        if re.search(rf"\b{re.escape(clean)}\b", lowered) and entity_id not in found:
            found.append(entity_id)
    return tuple(found)


def _base_filters(text: str, *, entity_names: dict[int, str], now: datetime) -> RouteFilters:
    source = "mail" if _MAIL_RE.search(text) else ""
    location = ""
    slide = _SLIDE_RE.search(text)
    if slide:
        location = f"{slide.group(1).lower()} {slide.group(2)}"
    doc_ref = ""
    ext = _URLS_OR_EXT_RE.search(text)
    if ext:
        doc_ref = ext.group(1).strip()
    time_from: datetime | None = None
    time_until: datetime | None = None
    if _TIME_TODAY_RE.search(text):
        time_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif _TIME_YESTERDAY_RE.search(text):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_from = start
        time_until = start + timedelta(days=1)
    elif _TIME_WEEK_RE.search(text):
        time_from = now - timedelta(days=7)
    return RouteFilters(
        source=source,
        doc_ref=doc_ref,
        location=location,
        time_from=time_from,
        time_until=time_until,
    )


def _with_entities(filters: RouteFilters, ids: tuple[int, ...]) -> RouteFilters:
    merged = tuple(dict.fromkeys(list(filters.entity_ids) + list(ids)))
    return RouteFilters(**{**filters.__dict__, "entity_ids": merged})


def _with_urgency(filters: RouteFilters, minimum: int) -> RouteFilters:
    return RouteFilters(**{**filters.__dict__, "urgency_min": max(filters.urgency_min, minimum)})


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


# -- rewrite ------------------------------------------------------------------


class RewriteCache:
    """The rewrite memo: normalized message + focus hash -> queries.

    The same question in the same conversation always rewrites the same
    way, so the work is done once and bounded — a small LRU, process only.
    """

    def __init__(self, capacity: int = 128) -> None:
        self._capacity = max(16, capacity)
        self._rows: OrderedDict[tuple[str, int], tuple[str, ...]] = OrderedDict()

    @staticmethod
    def focus_hash(focus: FocusState | None) -> int:
        if focus is None:
            return 0
        task = focus.open_task or {}
        attachment = focus.last_attachment or {}
        bits = (
            tuple(focus.recent_entities[:4]),
            str(task.get("summary") or "")[:120],
            str(attachment.get("filename") or "")[:120],
        )
        return hash(bits)

    def get(self, text: str, focus: FocusState | None) -> tuple[str, ...] | None:
        key = (_normalize(text), self.focus_hash(focus))
        found = self._rows.get(key)
        if found is not None:
            self._rows.move_to_end(key)
        return found

    def put(self, text: str, focus: FocusState | None, queries: tuple[str, ...]) -> None:
        key = (_normalize(text), self.focus_hash(focus))
        self._rows[key] = queries
        self._rows.move_to_end(key)
        while len(self._rows) > self._capacity:
            self._rows.popitem(last=False)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def rewrite(
    routing: Routing,
    focus: FocusState | None,
    *,
    entity_names: dict[int, str] | None = None,
    cache: RewriteCache | None = None,
) -> Routing:
    """Expand a short or anaphoric question, cached by message + focus.

    Weak input is a question carrying pronouns instead of nouns, or one
    too short to search well. Strong input keeps its own words: rewriting
    it would only dilute what the user carefully said.
    """
    text = routing.text
    words = [word.lower() for word in text.split()]
    weak = len(words) <= 3 or any(word.strip("!?.,") in _ANAPHORA for word in words)
    if not weak or not routing.needs_search:
        return Routing(**{**routing.__dict__, "queries": (text,) if text else ()})

    cached = cache.get(text, focus) if cache is not None else None
    if cached is not None:
        return Routing(**{**routing.__dict__, "queries": cached})

    names = entity_names or {}
    expansions: list[str] = [text]
    if focus is not None and focus.recent_entities:
        labels = [names.get(entity_id, "") for entity_id in focus.recent_entities[:2]]
        labels = [label for label in labels if label]
        if labels:
            expansions.append(f"{text} {' '.join(labels)}")
    if focus is not None and focus.open_task:
        summary = str(focus.open_task.get("summary") or "")[:120]
        if summary:
            expansions.append(f"{text} {summary}")
    if focus is not None and focus.last_attachment:
        filename = str(focus.last_attachment.get("filename") or "")
        if filename:
            expansions.append(f"{text} {filename}")
    exact = tuple(_exact_tokens(text))
    queries = tuple(_dedupe([expansion for expansion in expansions if expansion.strip()])[:3])
    if exact:
        queries = queries + (" ".join(exact),)
    if cache is not None:
        cache.put(text, focus, queries)
    return Routing(**{**routing.__dict__, "queries": queries, "exact_tokens": exact})


def _exact_tokens(text: str) -> list[str]:
    """The tokens a fuzzy search must not smooth away: filenames, numbers
    and capitalized names."""
    found: list[str] = []
    for match in re.findall(r"[\w@.\-/]+", text):
        if re.search(r"\d", match) or "." in match or match.isupper():
            found.append(match)
    return found[:10]
