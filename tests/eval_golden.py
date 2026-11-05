"""The golden set for the query pipeline (plan.md section 11).

Each case is the smallest world that poses one of the questions the plan
names: the events memory holds, the chunks retrieval can find, the focus
object a follow-up rides on, and what a correct answer has to cite. The
runner in test_eval_golden.py reports recall@5, MRR, route accuracy and
abstain precision/recall, and fails the build on a regression.

The set runs offline over the real components — routing rules, the lexical
index, fusion, the gate and packing — with a fake embedder where a case
needs the paraphrase signal. It is deliberately small and honest: every
case here is a question, never a setup the production path cannot produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aether.memory.focus import FocusState
from aether.memory.indexes import IndexedEvent
from aether.memory.vectors import MemoryHit

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


@dataclass(frozen=True)
class ChunkSpec:
    chunk_id: int
    event_id: int
    text: str
    source: str = "gmail"
    kind: str = "mail"
    filename: str = ""
    location: str = ""
    ordinal: int = 0


@dataclass(frozen=True)
class GoldenCase:
    name: str
    question: str
    events: list[IndexedEvent] = field(default_factory=list)
    chunks: list[ChunkSpec] = field(default_factory=list)
    focus: FocusState | None = None
    entity_names: dict[int, str] = field(default_factory=dict)
    expected_routes: tuple[str, ...] = ()
    expected_cited: frozenset[int] = frozenset()
    expected_resolved: str = ""
    expect_abstain: bool = False
    vector_hits: list[MemoryHit] = field(default_factory=list)


def _event(
    event_id: int,
    *,
    kind: str = "mail",
    source: str = "gmail",
    summary: str = "",
    urgency: int = 0,
    deadline: datetime | None = None,
    entities: tuple[int, ...] = (),
    payload: dict | None = None,
) -> IndexedEvent:
    return IndexedEvent(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=NOW,
        salience_score=6.0,
        memorable=True,
        payload=payload or {"sender": "Rahul <rahul@example.com>"},
        meta={},
        urgency=urgency,
        deadline_at=deadline,
        summary=summary,
        entity_ids=entities,
    )


def _doc_chunk(chunk_id: int, event_id: int, location: str, body: str) -> ChunkSpec:
    return ChunkSpec(
        chunk_id=chunk_id,
        event_id=event_id,
        text=f"[Doc: OSY.pdf | {location} | 2026-11-03] {body}",
        kind="shared_doc",
        filename="OSY.pdf",
        location=location,
        ordinal=int(location.split()[-1]),
    )


CASES: list[GoldenCase] = [
    # -- attention ---------------------------------------------------------
    GoldenCase(
        name="urgent_mail_right_after_arrival",
        question="is anything urgent?",
        events=[_event(1, urgency=3, summary="production is down — fix it now")],
        expected_routes=("attention",),
        expected_cited=frozenset({1}),
    ),
    GoldenCase(
        name="koi_urgent_hai",
        question="kuch urgent hai?",
        events=[_event(2, urgency=2, summary="send the report before noon")],
        expected_routes=("attention",),
        expected_cited=frozenset({2}),
    ),
    GoldenCase(
        name="anything_serious",
        question="anything serious?",
        events=[_event(6, urgency=2, summary="security review is overdue")],
        expected_routes=("attention",),
        expected_cited=frozenset({6}),
    ),
    # -- references --------------------------------------------------------
    GoldenCase(
        name="assignment_short_question",
        question="Assignment?",
        focus=FocusState(open_task={"commitment_id": 1, "summary": "finish the assignment today"}),
        expected_routes=("reference",),
        expected_resolved="task",
    ),
    GoldenCase(
        name="send_then",
        question="send then",
        focus=FocusState(
            last_attachment={"filename": "assignment.pdf", "event_id": 7, "media_id": 11}
        ),
        expected_routes=("reference",),
        expected_resolved="attachment",
    ),
    GoldenCase(
        name="send_assignment_then",
        question="send the assignment then",
        focus=FocusState(
            open_task={"commitment_id": 2, "summary": "finish the assignment today"},
            last_attachment={"filename": "assignment.pdf", "event_id": 7, "media_id": 11},
        ),
        expected_routes=("reference",),
        expected_resolved="task",
    ),
    GoldenCase(
        name="send_it_photos",
        question="send it",
        focus=FocusState(last_attachment={"filename": "photo.jpg", "event_id": 8, "media_id": 21}),
        expected_routes=("reference",),
        expected_resolved="attachment",
    ),
    # -- recall ------------------------------------------------------------
    GoldenCase(
        name="second_address_sender",
        question="what did vedant send from the other address?",
        chunks=[
            ChunkSpec(
                chunk_id=1,
                event_id=3,
                text="[Mail: Note | from Vedant <vedant@other.example> | 2026-11-03] "
                "A note sent from the other address.",
            )
        ],
        events=[_event(3)],
        expected_routes=("open_recall",),
        expected_cited=frozenset({3}),
    ),
    GoldenCase(
        name="relative_deadline_today",
        question="what's due today?",
        events=[
            _event(4, urgency=2, deadline=NOW + timedelta(hours=2), summary="submit the report")
        ],
        expected_routes=("attention",),
        expected_cited=frozenset({4}),
    ),
    GoldenCase(
        name="slide_3",
        question="what does slide 3 say?",
        events=[_event(5, kind="shared_doc", source="telegram")],
        chunks=[
            _doc_chunk(1, 5, "slide 2", "Deadlock needs a lock order."),
            _doc_chunk(2, 5, "slide 3", "Deadlock prevention requires a global order."),
        ],
        expected_routes=("doc",),
        expected_cited=frozenset({5}),
    ),
    GoldenCase(
        name="paraphrase_bill_versus_invoice",
        question="where is the bill?",
        events=[_event(9)],
        chunks=[
            ChunkSpec(
                chunk_id=3,
                event_id=9,
                text="[Mail: Invoice | from Rahul <rahul@example.com> | 2026-11-03] "
                "Your invoice payment is attached.",
            )
        ],
        vector_hits=[
            MemoryHit(
                event_id=9,
                chunk_id=3,
                source="gmail",
                kind="mail",
                filename="",
                location="",
                text="[Mail: Invoice | from Rahul <rahul@example.com> | 2026-11-03] "
                "Your invoice payment is attached.",
                score=0.72,
                occurred_at=NOW,
            )
        ],
        expected_routes=("open_recall",),
        expected_cited=frozenset({9}),
    ),
    GoldenCase(
        name="typo_invoice",
        question="where is the invoce due friday?",
        events=[_event(10)],
        chunks=[
            ChunkSpec(
                chunk_id=4,
                event_id=10,
                text="[Mail: Invoice | from Rahul <rahul@example.com> | 2026-11-03] "
                "The invoice is due friday.",
            )
        ],
        expected_routes=("open_recall",),
        expected_cited=frozenset({10}),
    ),
    GoldenCase(
        name="entity_timeline",
        question="everything with Rahul",
        events=[
            _event(11, summary="Rahul asked for the deck", entities=(5,)),
            _event(12, summary="Rahul confirmed the meeting", entities=(5,)),
        ],
        entity_names={5: "Rahul"},
        expected_routes=("person",),
        expected_cited=frozenset({11, 12}),
    ),
    GoldenCase(
        name="what_did_i_promise",
        question="what did I promise Rahul?",
        expected_routes=("commitment",),
    ),
    # -- no answer ---------------------------------------------------------
    GoldenCase(
        name="question_with_no_answer",
        question="what did the board decide about quantum computing?",
        events=[_event(13)],
        chunks=[
            ChunkSpec(
                chunk_id=5,
                event_id=13,
                text="[Mail: Invoice | from Rahul <rahul@example.com> | 2026-11-03] "
                "The invoice is due on Friday.",
            )
        ],
        expect_abstain=True,
    ),
]
