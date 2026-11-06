"""The structured views over the resident index: attention ordering and the
entity timeline.

These exercise the in-memory half only — the database half (commitments) is
covered by test_commitments.py, and the cold-scan path by the index tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aether.memory.facts import Fact
from aether.memory.indexes import IndexedEvent, MemoryIndex
from aether.memory.views import Views, wants_attention

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _event(
    event_id: int,
    *,
    urgency: int = 0,
    deadline: datetime | None = None,
    occurred_at: datetime | None = None,
    source: str = "gmail",
    kind: str = "mail",
    noise: bool = False,
    entities: tuple[int, ...] = (),
    summary: str = "",
) -> IndexedEvent:
    return IndexedEvent(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=occurred_at or NOW,
        salience_score=0.0,
        memorable=False,
        payload={},
        meta={},
        urgency=urgency,
        noise=noise,
        deadline_at=deadline,
        summary=summary,
        entity_ids=entities,
    )


def _index(*events: IndexedEvent) -> MemoryIndex:
    index = MemoryIndex()
    for event in events:
        index.upsert(event)
    return index


# ---------------------------------------------------------------------------
# wants_attention
# ---------------------------------------------------------------------------


def test_the_attention_trigger_matches_the_plan_words() -> None:
    assert wants_attention("is any urgent?") is True
    assert wants_attention("what's pending") is True
    assert wants_attention("anything due today?") is True
    assert wants_attention("deadline?") is True
    assert wants_attention("what's the weather") is False
    # an ordinary word that merely contains a trigger does not fire
    assert wants_attention("I doodled a robot") is False


# ---------------------------------------------------------------------------
# pending_attention
# ---------------------------------------------------------------------------


def test_attention_orders_urgency_then_deadline_then_recency() -> None:
    views = Views(
        _index(
            _event(1, urgency=1, occurred_at=NOW),
            _event(2, urgency=3, occurred_at=NOW),
            _event(3, urgency=3, deadline=NOW + timedelta(hours=2)),
            _event(4, urgency=3, deadline=NOW + timedelta(hours=1)),
            _event(
                5,
                urgency=3,
                deadline=NOW + timedelta(hours=1),
                occurred_at=NOW + timedelta(minutes=1),
            ),
        ),
        timezone="Asia/Kolkata",
    )

    order = [item.event_id for item in views.pending_attention(now=NOW)]

    # urgency 3 first; among them sooner deadline (1h) before later (2h);
    # among equal urgency and deadline, newer event first; urgency 1 last
    assert order == [5, 4, 3, 2, 1]


def test_attention_excludes_noise_and_low_urgency() -> None:
    views = Views(
        _index(
            _event(1, urgency=3),
            _event(2, urgency=3, noise=True),
            _event(3, urgency=0),
        ),
        timezone="Asia/Kolkata",
    )

    assert [item.event_id for item in views.pending_attention(now=NOW)] == [1]


def test_attention_renders_overdue_in_the_user_timezone() -> None:
    views = Views(
        _index(_event(1, urgency=3, deadline=NOW - timedelta(hours=1), summary="send it")),
        timezone="Asia/Kolkata",
    )

    text = views.render_attention(views.pending_attention(now=NOW), now=NOW)

    assert "overdue since" in text
    assert "send it" in text


def test_attention_payload_marks_overdue() -> None:
    views = Views(
        _index(_event(1, urgency=2, deadline=NOW - timedelta(minutes=1))),
        timezone="UTC",
    )

    payload = views.attention_payload(views.pending_attention(now=NOW), now=NOW)

    assert payload[0]["overdue"] is True


def test_commitments_render_with_due_and_overdue_state() -> None:
    from aether.memory.commitments import Commitment

    views = Views(_index(), timezone="UTC")
    items = [
        Commitment(
            id=1,
            kind="task",
            status="open",
            text="send the invoice",
            due_at=NOW - timedelta(hours=1),
            counterparty_identity_id=None,
            source_event_id=None,
            created_at=NOW,
        ),
        Commitment(
            id=2,
            kind="promise",
            status="open",
            text="review the deck",
            due_at=NOW + timedelta(hours=2),
            counterparty_identity_id=None,
            source_event_id=None,
            created_at=NOW,
        ),
    ]

    text = views.render_commitments(items, now=NOW)

    assert "overdue since" in text
    assert "due 2026-11-03 08:16" in text


# ---------------------------------------------------------------------------
# entity timeline
# ---------------------------------------------------------------------------


async def test_entity_timeline_uses_the_entity_map() -> None:
    views = Views(
        _index(
            _event(1, entities=(7,), occurred_at=NOW - timedelta(hours=2)),
            _event(2, entities=(7, 8), occurred_at=NOW - timedelta(hours=1)),
            _event(3, entities=(8,), occurred_at=NOW),
        ),
        timezone="UTC",
    )

    found = [event.id for event in await views.entity_timeline(7)]

    assert found == [2, 1]


async def test_entity_timeline_respects_since() -> None:
    views = Views(
        _index(
            _event(1, entities=(7,), occurred_at=NOW - timedelta(hours=2)),
            _event(2, entities=(7,), occurred_at=NOW - timedelta(hours=1)),
        ),
        timezone="UTC",
    )

    found = [event.id for event in await views.entity_timeline(7, since=NOW - timedelta(hours=1))]

    assert found == [2]


# ---------------------------------------------------------------------------
# the core facts block
# ---------------------------------------------------------------------------


class FakeFacts:
    def __init__(self, facts: list[Fact]) -> None:
        self._facts = facts

    async def active(self, now=None, limit=None) -> list[Fact]:
        return list(self._facts)


def _fact(key: str, value: str, *, type: str = "fact", subject: str = "user") -> Fact:
    return Fact(
        id=len(key) + len(value),
        subject_identity_id=None,
        subject=subject,
        key=key,
        value=value,
        type=type,
        status="active",
        valid_from=NOW - timedelta(days=1),
        valid_until=None,
        provenance=(1,),
        confidence=1.0,
        trusted=True,
        created_at=NOW,
    )


async def test_core_facts_keeps_rules_and_query_relevant_claims() -> None:
    views = Views(
        MemoryIndex(),
        facts=FakeFacts(
            [
                _fact("timezone", "Asia/Kolkata", type="preference"),
                _fact("invoice_due", "Friday"),
                _fact("tone", "plain and concise", type="rule"),
            ]
        ),
    )

    items = await views.core_fact_items("what is my timezone?")
    keys = [fact.key for fact in items]

    assert keys[0] == "tone"  # a standing rule always leads
    assert "timezone" in keys


async def test_core_facts_respects_the_character_budget() -> None:
    views = Views(
        MemoryIndex(),
        facts=FakeFacts(
            [
                _fact("a", "x" * 120),
                _fact("b", "y" * 120),
            ]
        ),
        core_facts_chars=200,
    )

    items = await views.core_fact_items("anything", limit_chars=200)

    assert len(items) == 1
