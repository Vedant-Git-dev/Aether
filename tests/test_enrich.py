"""Write-time enrichment: the floor, the deadline resolver, the classifier.

The floor and the resolver are deterministic and are the whole reason an
urgent mail survives a slow, rate-limited or absent model — so they are
tested directly, without a provider or a database. The classifier is
tested through a scripted double, including the case that matters most:
a classifier that fails must leave the event *scored by the floor*, not
unscored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.memory.enrich import (
    FLOOR_URGENCY,
    Enricher,
    EnricherError,
    Enrichment,
    ExtractedCommitment,
    LLMClassifier,
    heuristic_floor,
    normalize_deadline,
)
from aether.memory.indexes import IndexedEvent, MemoryIndex, decode_extra

# Tuesday, 11:46 in Asia/Kolkata — the event time every case resolves against
EVENT_AT = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
IST = "Asia/Kolkata"


def _at(phrase: str, *, occurred_at: datetime = EVENT_AT, timezone: str = IST):
    deadline = normalize_deadline(phrase, occurred_at=occurred_at, timezone=timezone)
    assert deadline is not None, phrase
    return deadline.at


def _ist(day: int, hour: int, minute: int = 0, *, month: int = 11, year: int = 2026) -> datetime:
    """An instant expressed in IST, as UTC — the assertions stay explicit
    about the zone instead of leaning on the machine's own locale."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# the heuristic floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please reply ASAP",
        "This is urgent",
        "URGENT: invoice overdue",
        "come immediately",
        "call me right away",
        "respond as soon as possible",
        "needs it by EOD",
        "by end of day please",
        "close of business today",
        "the deadline is tomorrow",
        "due today",
        "due tomorrow",
        "time-sensitive request",
        "action required",
    ],
)
def test_the_floor_catches_the_urgent_vocabulary(text: str) -> None:
    assert heuristic_floor(text) == FLOOR_URGENCY


@pytest.mark.parametrize(
    "text",
    [
        "reply within 2 hours",
        "in 3 days",
        "need this in 48 hours",
        "available in 2 weeks",
        "get back within 60 minutes",
    ],
)
def test_a_stated_window_is_urgent_even_without_the_word_deadline(text: str) -> None:
    assert heuristic_floor(text) == FLOOR_URGENCY


@pytest.mark.parametrize(
    "text",
    ["lunch tomorrow?", "here is the newsletter", "thanks, noted", "", "reading list for the week"],
)
def test_ordinary_text_is_not_urgent(text: str) -> None:
    assert heuristic_floor(text) == 0


def test_the_floor_is_case_insensitive() -> None:
    assert heuristic_floor("AsAp") == FLOOR_URGENCY


# ---------------------------------------------------------------------------
# the deadline resolver
# ---------------------------------------------------------------------------


def test_within_n_hours_resolves_from_the_event_time() -> None:
    assert _at("within 2 hours") == EVENT_AT + timedelta(hours=2)


def test_within_n_days_resolves_from_the_event_time() -> None:
    assert _at("in 3 days") == EVENT_AT + timedelta(days=3)


def test_by_eod_is_the_working_day_close_in_the_users_timezone() -> None:
    assert _at("by EOD") == _ist(3, 17)
    assert _at("end of day") == _ist(3, 17)


def test_eod_said_after_the_working_day_means_tomorrow() -> None:
    late = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)  # 19:30 IST
    assert normalize_deadline("by EOD", occurred_at=late, timezone=IST).at == _ist(4, 17)


def test_tomorrow_is_the_next_days_close() -> None:
    assert _at("tomorrow") == _ist(4, 17)


def test_tonight_never_rolls_forward() -> None:
    # an expired "tonight" must read as overdue rather than become tomorrow
    late = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)  # 23:30 IST
    assert normalize_deadline("tonight", occurred_at=late, timezone=IST).at == _ist(3, 21)


def test_a_named_day_is_the_next_one_never_today() -> None:
    assert _at("by Friday") == _ist(6, 17)
    # said on a Friday, "by Friday" is the Friday coming
    friday = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    assert normalize_deadline("by Friday", occurred_at=friday, timezone=IST).at == _ist(13, 17)


def test_next_week_resolves_to_a_range() -> None:
    deadline = normalize_deadline("next week", occurred_at=EVENT_AT, timezone=IST)
    assert deadline.at == _ist(9, 9)  # the coming Monday, 9am
    assert deadline.end == _ist(13, 17)  # through Friday's close


def test_an_absolute_date_ends_at_the_working_day_close() -> None:
    assert _at("2026-11-05") == _ist(5, 17)
    assert _at("by Nov 5") == _ist(5, 17)
    assert _at("Nov 5") == _ist(5, 17)


def test_a_yearless_date_rolls_forward_when_it_has_passed() -> None:
    assert _at("Jan 5") == _ist(5, 17, month=1, year=2027)
    assert _at("Dec 25") == _ist(25, 17, month=12)


def test_a_clock_time_is_used_when_the_phrase_gives_one() -> None:
    assert _at("by 6pm") == _ist(3, 18)
    assert _at("at 18:30") == _ist(3, 18, 30)
    assert _at("by Nov 5 at 3pm") == _ist(5, 15)


def test_a_clock_already_past_means_tomorrow() -> None:
    late = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)  # 19:30 IST, past 6pm
    assert normalize_deadline("by 6pm", occurred_at=late, timezone=IST).at == _ist(4, 18)


def test_the_original_words_travel_with_the_result() -> None:
    assert normalize_deadline("by end of day", occurred_at=EVENT_AT).phrase == "by end of day"


def test_an_unreadable_phrase_resolves_to_nothing() -> None:
    # not guessed at — the caller keeps the words and moves on
    assert normalize_deadline("sometime soon", occurred_at=EVENT_AT) is None
    assert normalize_deadline("", occurred_at=EVENT_AT) is None
    assert normalize_deadline("   ", occurred_at=EVENT_AT) is None


def test_a_naive_event_time_is_read_as_utc() -> None:
    naive = datetime(2026, 11, 3, 6, 16)
    assert normalize_deadline("by EOD", occurred_at=naive, timezone=IST).at == _ist(3, 17)


def test_an_unknown_timezone_falls_back_to_utc() -> None:
    deadline = normalize_deadline("by EOD", occurred_at=EVENT_AT, timezone="Mars/Olympus")
    assert deadline.at == datetime(2026, 11, 3, 17, 0, tzinfo=UTC)


def test_an_expired_deadline_reads_as_past() -> None:
    deadline = normalize_deadline("within 2 hours", occurred_at=EVENT_AT, timezone=IST)
    assert deadline.is_past(EVENT_AT + timedelta(hours=3)) is True
    assert deadline.is_past(EVENT_AT + timedelta(hours=1)) is False


# ---------------------------------------------------------------------------
# the enrichment record
# ---------------------------------------------------------------------------


def test_the_earliest_deadline_is_what_due_means() -> None:
    first = normalize_deadline("within 2 hours", occurred_at=EVENT_AT)
    later = normalize_deadline("in 5 days", occurred_at=EVENT_AT)
    enrichment = Enrichment(deadlines=[later, first])
    assert enrichment.deadline_at == first.at
    assert enrichment.deadline_phrase == "within 2 hours"


def test_a_range_contributes_its_end_to_the_window() -> None:
    deadline = normalize_deadline("next week", occurred_at=EVENT_AT)
    enrichment = Enrichment(deadlines=[deadline])
    assert enrichment.deadline_end == deadline.end


def test_no_deadlines_means_no_due_time_not_a_guess() -> None:
    enrichment = Enrichment()
    assert enrichment.deadline_at is None
    assert enrichment.deadline_end is None
    assert enrichment.deadline_phrase == ""


# ---------------------------------------------------------------------------
# the classifier, through the enricher
# ---------------------------------------------------------------------------


class FakeClassifier:
    """Scripted classifier: returns `readings[i]`, or raises it."""

    def __init__(self, readings: list[object]) -> None:
        self.readings = list(readings)
        self.texts: list[str] = []

    async def classify(self, text: str) -> dict:
        self.texts.append(text)
        if not self.readings:
            raise AssertionError("FakeClassifier ran out of readings")
        item = self.readings.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _enricher(*readings: object) -> tuple[Enricher, FakeClassifier]:
    classifier = FakeClassifier(list(readings))
    return Enricher(classifier, timezone=IST), classifier


async def test_a_successful_classification_fills_every_field() -> None:
    enricher, _ = _enricher(
        {
            "urgency": 3,
            "needs_reply": True,
            "action_type": "pay",
            "deadlines": ["by EOD"],
            "commitments": [{"kind": "request", "text": "send the invoice", "due": "by EOD"}],
            "summary": "Rahul asks for the October invoice",
            "noise": False,
        }
    )

    result = await enricher.enrich("please send the invoice", occurred_at=EVENT_AT)

    assert result.classified is True
    assert result.urgency == 3
    assert result.needs_reply is True
    assert result.action_type == "pay"
    assert result.summary == "Rahul asks for the October invoice"
    assert result.deadline_at == _ist(3, 17)
    assert result.commitments == [
        ExtractedCommitment(kind="request", text="send the invoice", due_at=_ist(3, 17))
    ]


async def test_a_failed_classifier_keeps_the_floor() -> None:
    enricher, _ = _enricher(RuntimeError("provider down"))

    result = await enricher.enrich("this is urgent", occurred_at=EVENT_AT)

    # the point of the floor: the event is still scored, just not summarised
    assert result.urgency == FLOOR_URGENCY
    assert result.classified is False
    assert result.summary == ""


async def test_the_model_cannot_lower_the_floor() -> None:
    # a classifier that reads "URGENT" as routine must not unhide it
    enricher, _ = _enricher({"urgency": 0, "summary": "newsletter"})

    result = await enricher.enrich("URGENT: reply now", occurred_at=EVENT_AT)

    assert result.urgency == FLOOR_URGENCY


async def test_urgency_is_clamped_and_junk_degrades() -> None:
    enricher, _ = _enricher(
        {
            "urgency": 99,
            "needs_reply": "yes",
            "action_type": "x" * 200,
            "deadlines": "not a list",
            "commitments": "not a list",
            "summary": "s" * 500,
        }
    )

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    assert result.urgency == 3
    assert result.deadlines == []
    assert result.commitments == []
    assert len(result.summary) == 140
    assert len(result.action_type) == 40


async def test_a_deadline_phrase_the_resolver_cannot_read_is_dropped() -> None:
    enricher, _ = _enricher({"urgency": 1, "deadlines": ["sometime soon", "by EOD"]})

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    # the unreadable one is dropped rather than stored as a guess
    assert [deadline.phrase for deadline in result.deadlines] == ["by EOD"]


async def test_a_repeated_deadline_window_collapses() -> None:
    enricher, _ = _enricher({"urgency": 1, "deadlines": ["by EOD", "end of day"]})

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    assert len(result.deadlines) == 1


async def test_a_bare_string_commitment_is_read_as_a_task() -> None:
    enricher, _ = _enricher({"urgency": 1, "commitments": ["send the deck"]})

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    assert result.commitments == [ExtractedCommitment(kind="task", text="send the deck")]


async def test_an_unknown_commitment_kind_falls_back_to_task() -> None:
    enricher, _ = _enricher({"urgency": 1, "commitments": [{"kind": "vibe", "text": "x"}]})

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    assert result.commitments[0].kind == "task"


async def test_a_commitment_with_no_text_is_dropped() -> None:
    enricher, _ = _enricher({"urgency": 1, "commitments": [{"kind": "task", "text": "  "}]})

    result = await enricher.enrich("hello", occurred_at=EVENT_AT)

    assert result.commitments == []


# ---------------------------------------------------------------------------
# the classifier adapter
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, list, list]] = []

    async def complete(self, system, messages, tools):
        self.calls.append((system, messages, tools))
        from aether.llm.types import Turn

        return Turn(text=self.text)


async def test_the_classifier_adapter_asks_one_toolless_turn() -> None:
    provider = _StubProvider('{"urgency": 2, "summary": "hi"}')

    data = await LLMClassifier(provider).classify("some text")

    assert data["urgency"] == 2
    assert len(provider.calls) == 1
    assert provider.calls[0][2] == []  # no tools: it is a reading, not an act


async def test_the_classifier_adapter_rejects_a_non_object() -> None:
    # one error contract for unusable output, whatever shape the model answered in
    with pytest.raises(EnricherError):
        await LLMClassifier(_StubProvider("[1, 2, 3]")).classify("x")
    with pytest.raises(EnricherError):
        await LLMClassifier(_StubProvider("I'm afraid I can't do that")).classify("x")


# ---------------------------------------------------------------------------
# the index's view of the sealed extra
# ---------------------------------------------------------------------------


def test_decode_extra_reads_the_enrichment_fields() -> None:
    decoded = decode_extra(
        {
            "urgency": 3,
            "needs_reply": True,
            "noise": False,
            "deadline_at": "2026-11-03T11:30:00+00:00",
            "summary": "invoice",
            "entities": [4, 7, 4],
        }
    )

    assert decoded["urgency"] == 3
    assert decoded["needs_reply"] is True
    assert decoded["deadline_at"] == datetime(2026, 11, 3, 11, 30, tzinfo=UTC)
    assert decoded["summary"] == "invoice"
    assert decoded["entity_ids"] == (4, 7)  # deduped, order kept


def test_decode_extra_degrades_junk_to_defaults() -> None:
    decoded = decode_extra(
        {
            "urgency": "very",
            "deadline_at": "not a time",
            "entities": ["x", None, 3],
            "summary": None,
        }
    )

    assert decoded == {
        "urgency": 0,
        "needs_reply": False,
        "noise": False,
        "deadline_at": None,
        "summary": "",
        "entity_ids": (3,),
    }


def test_decode_extra_on_nothing_is_all_defaults() -> None:
    assert decode_extra(None)["urgency"] == 0
    assert decode_extra("nonsense")["entity_ids"] == ()


def test_decode_extra_clamps_urgency_into_range() -> None:
    assert decode_extra({"urgency": 99})["urgency"] == 3
    assert decode_extra({"urgency": -5})["urgency"] == 0


# ---------------------------------------------------------------------------
# the index's entity map
# ---------------------------------------------------------------------------


def _event(event_id: int, *, entities: tuple[int, ...] = (), when: datetime = EVENT_AT):
    return IndexedEvent(
        id=event_id,
        source="gmail",
        kind="mail",
        occurred_at=when,
        salience_score=0.0,
        memorable=False,
        entity_ids=entities,
    )


def test_events_for_entity_returns_newest_first() -> None:
    index = MemoryIndex()
    index.upsert(_event(1, entities=(4,), when=EVENT_AT))
    index.upsert(_event(2, entities=(4,), when=EVENT_AT + timedelta(hours=1)))
    index.upsert(_event(3, entities=(9,), when=EVENT_AT))

    assert [event.id for event in index.events_for_entity(4)] == [2, 1]
    assert [event.id for event in index.events_for_entity(9)] == [3]
    assert index.events_for_entity(99) == []


def test_an_enrichment_write_moves_an_event_between_entities() -> None:
    index = MemoryIndex()
    index.upsert(_event(1))

    index.update_extra(1, {"entities": [4], "urgency": 2})
    assert [event.id for event in index.events_for_entity(4)] == [1]
    assert index.get(1).urgency == 2

    # the entity linking is corrected later — the stale link must not linger
    index.update_extra(1, {"entities": [9]})
    assert index.events_for_entity(4) == []
    assert [event.id for event in index.events_for_entity(9)] == [1]


def test_a_salience_write_keeps_the_entity_map() -> None:
    index = MemoryIndex()
    index.upsert(_event(1, entities=(4,)))

    index.update_salience(1, 9.0, True, {"entities": [4], "summary": "kept"})

    assert [event.id for event in index.events_for_entity(4)] == [1]
    assert index.get(1).memorable is True
    assert index.get(1).summary == "kept"


def test_trimming_the_hot_set_drops_the_entity_links() -> None:
    index = MemoryIndex()
    for event_id in range(1, 6):
        index.upsert(_event(event_id, entities=(4,), when=EVENT_AT + timedelta(minutes=event_id)))

    index._trim(2)

    assert [event.id for event in index.events_for_entity(4)] == [5, 4]
