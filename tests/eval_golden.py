"""The golden set for the query pipeline (plan.md section 11).

Each case is the smallest world that poses one of the questions the plan
names: the events memory holds, the chunks retrieval can find, the focus
object a follow-up rides on, and what a correct answer has to cite. The
runner in test_eval_golden.py reports recall@5, MRR, route accuracy and
abstain precision/recall, and fails the build on a regression. The
two-arm runner in eval_ablation.py reuses the same cases to measure the
vector path against lexical retrieval and to gate the embedding decision.

Every case carries a `subset` so the evaluation can report per-question
shape instead of one blended number, and an `answer` for the claim-support
arm: an honest reply the verifier must find fully supported by the pack.

The set runs offline over the real components — routing rules, the lexical
index, fusion, the gate and packing — with a fake embedder where a case
needs the paraphrase signal. It is deliberately small and honest: every
case here is a question, never a setup the production path cannot produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aether.memory.commitments import OPEN, Commitment
from aether.memory.focus import FocusState
from aether.memory.indexes import IndexedEvent
from aether.memory.mail import render_mail_event
from aether.memory.vectors import MemoryHit

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
# Tuesday 17:00 in the user's timezone, the instant "by Friday" resolves to
FRIDAY_CLOSE_UTC = datetime(2026, 11, 6, 11, 30, tzinfo=UTC)
OWNER_PRIMARY = "vedant@example.com"
OWNER_SECOND = "vedant.work@example.com"


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
    subset: str = ""
    # an honest reply the claim-support check runs through memory/verify.py
    answer: str = ""
    events: list[IndexedEvent] = field(default_factory=list)
    chunks: list[ChunkSpec] = field(default_factory=list)
    focus: FocusState | None = None
    entity_names: dict[int, str] = field(default_factory=dict)
    expected_routes: tuple[str, ...] = ()
    expected_cited: frozenset[int] = frozenset()
    expected_resolved: str = ""
    expect_abstain: bool = False
    vector_hits: list[MemoryHit] = field(default_factory=list)
    # an ambiguous reference resolves to a question naming the options
    expected_ask_terms: tuple[str, ...] = ()
    # the packed block must (or must never) carry these plain strings
    expected_block_terms: tuple[str, ...] = ()
    expected_block_absent: tuple[str, ...] = ()
    # the open commitments the golden Views reads, if the case has any
    commitments: list[Commitment] = field(default_factory=list)
    # the owner's addresses, so mail rendering keeps "(your other address)"
    owner_addresses: frozenset[str] = frozenset()
    # the deadline arm: a phrase resolved against the case clock
    deadline_phrase: str = ""
    deadline_at: datetime | None = None
    deadline_render_at: datetime | None = None
    deadline_render: str = ""


class FakeCommitments:
    """The open set for a golden case: no database, the surface Views reads."""

    def __init__(self, items: list[Commitment] | None = None) -> None:
        self.items = list(items or [])

    async def list_open(self, limit: int = 50) -> list[Commitment]:
        return self.items[:limit]


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


def _owner_mail_chunk() -> ChunkSpec:
    """The chunk the lexical index would hold for a mail from the owner's
    second address: the real renderer writes it, so the case cannot flatter
    the pipeline with hand-written text."""
    text = render_mail_event(
        {
            "subject": "Note to self",
            "sender": f"Vedant <{OWNER_SECOND}>",
            "to": "Rahul <rahul@example.com>",
            "body": "Remember to send the deck from my second address.",
        },
        occurred_at=NOW,
        owners={OWNER_SECOND},
    )
    return ChunkSpec(chunk_id=6, event_id=14, text=text, source="gmail", kind="mail")


CASES: list[GoldenCase] = [
    # -- attention ---------------------------------------------------------
    GoldenCase(
        name="urgent_mail_right_after_arrival",
        question="is anything urgent?",
        subset="attention",
        answer="Yes — the production outage needs you now [E1].",
        events=[_event(1, urgency=3, summary="production is down — fix it now")],
        expected_routes=("attention",),
        expected_cited=frozenset({1}),
    ),
    GoldenCase(
        name="koi_urgent_hai",
        question="kuch urgent hai?",
        subset="attention",
        answer="Yes — the report needs to go out before noon [E2].",
        events=[_event(2, urgency=2, summary="send the report before noon")],
        expected_routes=("attention",),
        expected_cited=frozenset({2}),
    ),
    GoldenCase(
        name="anything_serious",
        question="anything serious?",
        subset="attention",
        answer="Yes — the security review is overdue [E6].",
        events=[_event(6, urgency=2, summary="security review is overdue")],
        expected_routes=("attention",),
        expected_cited=frozenset({6}),
    ),
    # -- references --------------------------------------------------------
    GoldenCase(
        name="assignment_short_question",
        question="Assignment?",
        subset="reference",
        focus=FocusState(open_task={"commitment_id": 1, "summary": "finish the assignment today"}),
        expected_routes=("reference",),
        expected_resolved="task",
    ),
    GoldenCase(
        name="send_then",
        question="send then",
        subset="reference",
        focus=FocusState(
            last_attachment={"filename": "assignment.pdf", "event_id": 7, "media_id": 11}
        ),
        expected_routes=("reference",),
        expected_resolved="attachment",
    ),
    GoldenCase(
        name="send_assignment_then",
        question="send the assignment then",
        subset="reference",
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
        subset="reference",
        focus=FocusState(last_attachment={"filename": "photo.jpg", "event_id": 8, "media_id": 21}),
        expected_routes=("reference",),
        expected_resolved="attachment",
    ),
    GoldenCase(
        name="send_it_several_photos",
        question="send it",
        subset="reference",
        focus=FocusState(
            recent_attachments=[
                {"filename": "photo-one.jpg", "event_id": 8, "media_id": 21},
                {"filename": "photo-two.jpg", "event_id": 8, "media_id": 22},
            ],
            last_attachment={"filename": "photo-one.jpg", "event_id": 8, "media_id": 21},
        ),
        expected_routes=("reference",),
        expected_ask_terms=("photo-one.jpg", "photo-two.jpg"),
    ),
    # -- recall ------------------------------------------------------------
    GoldenCase(
        name="second_address_sender",
        question="what did vedant send from the other address?",
        subset="recall",
        answer="A note was sent from Vedant's other address [E3].",
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
        name="owner_second_address",
        question="what did I send from my second address?",
        subset="recall",
        answer="You sent a note to Rahul from your second address [E14].",
        events=[
            _event(
                14,
                summary="a note to Rahul",
                payload={"sender": f"Vedant <{OWNER_SECOND}>"},
            )
        ],
        chunks=[_owner_mail_chunk()],
        owner_addresses=frozenset({OWNER_SECOND}),
        expected_routes=("open_recall",),
        expected_cited=frozenset({14}),
        expected_block_terms=("(your other address)", OWNER_SECOND),
        expected_block_absent=(OWNER_PRIMARY,),
    ),
    GoldenCase(
        name="relative_deadline_today",
        question="what's due today?",
        subset="deadline",
        events=[
            _event(4, urgency=2, deadline=NOW + timedelta(hours=2), summary="submit the report")
        ],
        expected_routes=("attention",),
        expected_cited=frozenset({4}),
    ),
    GoldenCase(
        name="relative_deadline_absolute",
        question="what's due today?",
        subset="deadline",
        answer="The invoice is due Friday at 17:00 [E15].",
        events=[
            _event(
                15,
                urgency=2,
                deadline=FRIDAY_CLOSE_UTC,
                summary="send the invoice",
            )
        ],
        expected_routes=("attention",),
        expected_cited=frozenset({15}),
        deadline_phrase="by Friday",
        deadline_at=FRIDAY_CLOSE_UTC,
        deadline_render_at=FRIDAY_CLOSE_UTC + timedelta(hours=1),
        deadline_render="overdue since 2026-11-06 17:00",
    ),
    GoldenCase(
        name="slide_3",
        question="what does slide 3 say?",
        subset="doc",
        answer="Slide 3 says deadlock prevention requires a global order [E5].",
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
        subset="paraphrase",
        answer="Rahul sent the invoice payment as an attachment [E9].",
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
        subset="recall",
        answer="The invoice is due on Friday [E10].",
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
        subset="recall",
        answer="Rahul asked for the deck and then confirmed the meeting [E11].",
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
        subset="commitment",
        expected_routes=("commitment",),
    ),
    GoldenCase(
        name="promise_with_store",
        question="what did I promise Rahul?",
        subset="commitment",
        answer="You promised to send Rahul the deck by Friday [E12].",
        events=[_event(12, summary="Rahul asked for the deck")],
        commitments=[
            Commitment(
                id=1,
                kind="promise",
                status=OPEN,
                text="send Rahul the deck",
                due_at=FRIDAY_CLOSE_UTC,
                counterparty_identity_id=5,
                source_event_id=12,
                created_at=NOW,
            )
        ],
        expected_routes=("commitment",),
        expected_block_terms=("send Rahul the deck",),
    ),
    # -- no answer ---------------------------------------------------------
    GoldenCase(
        name="question_with_no_answer",
        question="what did the board decide about quantum computing?",
        subset="abstain",
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
