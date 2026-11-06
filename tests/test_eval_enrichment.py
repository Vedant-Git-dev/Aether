"""The enrichment arm (plan.md section 11).

The golden set never touches `EventStore`, so write-time enrichment needs an
arm of its own: ingest the same small world twice — once with the flag off,
once on — and answer the plan's attention, deadline and commitment questions
from each. The floor arm must never lose an urgent item; the enriched arm
must gain the deadlines and promises the floor cannot see.

The classifier here is a deterministic stand-in, not the model. It measures
the *wiring* — that a reading turns text into structured fields, lands in
the sealed meta, and reaches the views without a second call — and it counts
its calls so the per-event cost is a number rather than a guess. It is not
evidence about the real classifier's quality, and `docs/evaluation.md` says
so: the default stays `false` until a live run clears
`memory.enrichment_min_answer_gain`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig, MemoryConfig
from aether.memory.commitments import Commitments
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.enrich import Enricher
from aether.memory.events import EventStore
from aether.memory.indexes import MemoryIndex
from aether.memory.views import Views

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)  # Tuesday, 11:46 IST
TIMEZONE = "Asia/Kolkata"


class ReadingClassifier:
    """A deterministic stand-in for the model, keyed off the event words."""

    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, text: str) -> dict:
        self.calls += 1
        reading: dict = {"summary": " ".join(text.split())[:60]}
        if "production" in text:
            reading["urgency"] = 3
        if "invoice" in text:
            reading["urgency"] = 2
            reading["deadlines"] = ["by Friday"]
        if "report" in text:
            reading["commitments"] = [
                {"kind": "promise", "text": "send the report", "due": "by Friday"}
            ]
        return reading


def _mail(subject: str, body: str) -> dict:
    return {
        "subject": subject,
        "sender": "Rahul Sharma <rahul@example.com>",
        "to": "me@example.com",
        "cc": "",
        "body": body,
    }


# (label, the question it stands for, the event that answers it, the words)
WORLD = [
    ("outage", "is anything urgent?", "production is down — fix it immediately"),
    ("invoice", "what's due?", "The invoice is due by Friday."),
    ("report", "what did I promise?", "I'll send the report by Friday."),
]


@dataclass
class Arm:
    events: EventStore
    index: MemoryIndex
    commitments: Commitments
    views: Views
    classifier: ReadingClassifier | None


async def _arm(db, *, enrichment: bool) -> Arm:
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    index = MemoryIndex()
    commitments = Commitments(db, cipher, audit)
    classifier = ReadingClassifier()
    events = EventStore(db, cipher, audit, ContactsConfig(), index=index)
    events.attach_enrichment(
        commitments=commitments,
        enricher=Enricher(classifier, timezone=TIMEZONE),
        enrichment=enrichment,
    )
    views = Views(index, commitments, timezone=TIMEZONE)
    return Arm(events, index, commitments, views, classifier)


async def _ingest(arm: Arm) -> dict[str, int]:
    """One event per question, ingested through the real store; returns the
    label -> event id map for the measurements below."""
    ids: dict[str, int] = {}
    for label, _question, body in WORLD:
        result = await arm.events.ingest(
            source="gmail",
            kind="mail",
            payload=_mail(label, body),
            occurred_at=NOW,
        )
        assert result.stored and result.event_id is not None
        ids[label] = result.event_id
    await arm.events.wait_idle()
    return ids


async def _answers(arm: Arm, ids: dict[str, int]) -> dict[str, bool]:
    """The three questions answered from the resident views."""
    attention = await _attention_ids(arm, ids)
    deadlines = await _deadline_ids(arm, ids)
    promises = await _promise_ids(arm, ids)
    return {
        "attention": ids["outage"] in attention,
        "deadline": ids["invoice"] in deadlines,
        "commitment": ids["report"] in promises,
    }


async def _attention_ids(arm: Arm, ids: dict[str, int]) -> set[int]:
    return {item.event_id for item in arm.views.pending_attention(now=NOW, min_urgency=1)}


async def _deadline_ids(arm: Arm, ids: dict[str, int]) -> set[int]:
    return {
        item.event_id
        for item in arm.views.pending_attention(now=NOW, min_urgency=1)
        if item.deadline_at is not None
    }


async def _promise_ids(arm: Arm, ids: dict[str, int]) -> set[int]:
    found = await arm.commitments.list_open(limit=10)
    return {commitment.source_event_id for commitment in found}


def _rate(answers: dict[str, bool]) -> float:
    return sum(1 for value in answers.values() if value) / len(answers)


async def test_enrichment_off_answers_only_the_floor_and_never_loses_an_urgent(
    db,
) -> None:
    off = await _arm(db, enrichment=False)
    ids = await _ingest(off)
    answers = await _answers(off, ids)

    # the deterministic floor keeps the outage answerable with no model at all
    assert answers["attention"] is True
    # and the classifier never ran
    assert off.classifier is not None and off.classifier.calls == 0


async def test_enrichment_on_reads_deadlines_and_promises_the_floor_cannot(db) -> None:
    off = await _arm(db, enrichment=False)
    on = await _arm(db, enrichment=True)
    off_ids = await _ingest(off)
    on_ids = await _ingest(on)

    off_answers = await _answers(off, off_ids)
    on_answers = await _answers(on, on_ids)

    # the floor arm's own urgent item is not lost by turning enrichment on
    assert on_answers["attention"] is True
    # the enriched arm sees the invoice's deadline and the promise
    assert off_answers["deadline"] is False and on_answers["deadline"] is True
    assert off_answers["commitment"] is False and on_answers["commitment"] is True

    gain = _rate(on_answers) - _rate(off_answers)
    cost = on.classifier.calls if on.classifier is not None else 0
    print(f"enrichment answer gain={gain:.3f} classifier calls={cost}/{len(WORLD)}")

    # exactly one classifier call per event — the bounded cost the plan names
    assert cost == len(WORLD)
    assert gain > 0


def test_the_default_is_the_decision_not_the_arm() -> None:
    # the offline arm uses a stand-in classifier, so it can never justify
    # flipping the default on its own; this pins that the default remains
    # the deliberate opt-in until a live run clears the configured margin
    assert MemoryConfig().enrichment is False
