"""Write-time enrichment at the ingest path.

The properties that matter here are the ones that keep the pipeline honest
when something is slow or broken:

  - the floor is stored before anything else runs, so an urgent mail is
    never invisible;
  - the classifier rides behind the flag, and its failure costs detail,
    never the event;
  - entity linking and the owner-alias rules do not ride that flag, because
    the rendering rules depend on them;
  - two spawned writers (salience and enrichment) never erase each other.

The pure helpers are unit-tested; everything that needs a database is
marked integration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fakes import FakeEmbedder, FakePersonJudge

from aether.authz.audit import AuditLog
from aether.config import ContactsConfig
from aether.memory.commitments import Commitments
from aether.memory.context import ContextBuilder, alias_question_text
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.enrich import Enricher, Enrichment, enrichment_text
from aether.memory.entities import Entities, Sender
from aether.memory.events import EventStore
from aether.memory.facts import Facts
from aether.memory.indexes import MemoryIndex
from aether.memory.mail import MAIL_KIND, MAIL_SOURCE
from aether.memory.vectors import VectorMemory

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


class ScriptedClassifier:
    """A classifier double: one scripted reading, or a failure."""

    def __init__(self, reading: object) -> None:
        self.reading = reading
        self.calls: list[str] = []

    async def classify(self, text: str) -> dict:
        self.calls.append(text)
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


def _mail(**overrides) -> dict:
    payload = {
        "message_id": "m1",
        "thread_id": "t1",
        "subject": "Invoice for October",
        "sender": "Rahul Sharma <rahul@example.com>",
        "to": "me@example.com",
        "cc": "",
        "date": "Tue, 03 Nov 2026 11:46:00 +0530",
        "body": "Please find the invoice attached.",
    }
    payload.update(overrides)
    return payload


async def _wire(
    db,
    *,
    reading: object = None,
    enrichment: bool = False,
    embedder=None,
    index: MemoryIndex | None = None,
):
    """An EventStore with the real collaborators over one fresh key."""
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    entities = Entities(db, cipher, audit, FakePersonJudge([]))
    commitments = Commitments(db, cipher, audit)
    vectors = VectorMemory(db, cipher, embedder) if embedder is not None else None
    events = EventStore(
        db,
        cipher,
        audit,
        ContactsConfig(),
        indexer=vectors,
        index=index,
    )
    events.attach_enrichment(
        entities=entities,
        commitments=commitments,
        enricher=Enricher(ScriptedClassifier(reading)) if reading is not None else None,
        enrichment=enrichment,
    )
    return events, entities, commitments


# ---------------------------------------------------------------------------
# the text an event is understood from
# ---------------------------------------------------------------------------


def test_a_mail_reads_as_its_subject_sender_snippet_and_body() -> None:
    text = enrichment_text(MAIL_KIND, _mail())
    assert "Invoice for October" in text
    assert "rahul@example.com" in text
    assert "Please find the invoice attached." in text


def test_a_chat_message_reads_as_what_was_said() -> None:
    assert enrichment_text("chat_message", {"text": "hey", "chat_ref": "c1"}) == "hey"


def test_a_document_reads_as_its_name_caption_and_text() -> None:
    text = enrichment_text("shared_doc", {"filename": "OSY.pdf", "text": "deadlocks"})
    assert "OSY.pdf" in text and "deadlocks" in text


def test_anything_else_falls_back_to_its_own_rendering() -> None:
    assert "poll" in enrichment_text("poll:new_events", {"result": "poll"})


# ---------------------------------------------------------------------------
# the floor (no database needed)
# ---------------------------------------------------------------------------


def test_the_reading_projects_only_what_was_learned() -> None:
    extra = Enrichment(urgency=0).to_extra()
    assert extra == {"urgency": 0}
    assert "summary" not in extra and "needs_reply" not in extra


def test_a_deadline_range_carries_its_end() -> None:
    from aether.memory.enrich import normalize_deadline

    range_deadline = normalize_deadline("next week", occurred_at=NOW)
    extra = Enrichment(urgency=1, deadlines=[range_deadline]).to_extra()
    assert extra["deadline_at"] == range_deadline.at.isoformat()
    assert extra["deadline_end"] == range_deadline.end.isoformat()
    assert extra["deadline_phrase"] == "next week"


def test_a_single_deadline_carries_no_range_end() -> None:
    from aether.memory.enrich import normalize_deadline

    single = normalize_deadline("by EOD", occurred_at=NOW)
    extra = Enrichment(urgency=2, deadlines=[single]).to_extra()
    assert extra["deadline_at"] == single.at.isoformat()
    assert "deadline_end" not in extra


def test_the_alias_question_names_the_address_and_says_how_to_answer() -> None:
    text = alias_question_text({"address": "vedant@work.com", "name": "Vedant"})
    assert "vedant@work.com" in text
    assert "owner_alias" in text
    assert "ask once" in text


# ---------------------------------------------------------------------------
# waiting for enrichment without cancelling it
# ---------------------------------------------------------------------------


async def test_wait_for_events_returns_pending_without_cancelling_the_work() -> None:
    store = EventStore(None, None, None, ContactsConfig())
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    task = asyncio.create_task(work())
    store._enriching[7] = task
    try:
        pending = await store.wait_for_events([7], timeout=0.01)
        assert pending == {7}
        assert not task.done()
    finally:
        release.set()
        await task
        store._enriching.pop(7, None)


async def test_cancelling_wait_idle_leaves_the_enrichment_running() -> None:
    store = EventStore(None, None, None, ContactsConfig())
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    task = asyncio.create_task(work())
    store._enriching[7] = task
    waiter = asyncio.create_task(store.wait_idle())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    try:
        assert not task.done()
    finally:
        release.set()
        await task
        store._enriching.pop(7, None)


# ---------------------------------------------------------------------------
# the floor at ingest (real store, no classifier)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_an_urgent_mail_is_scored_by_the_floor_with_no_enricher(db) -> None:
    events, _, _ = await _wire(db)

    result = await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(body="Please reply ASAP"),
        occurred_at=NOW,
    )
    await events.wait_idle()

    assert result.event_id is not None
    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.urgency == 2


@pytest.mark.integration
async def test_an_ordinary_message_is_not_flagged_by_the_floor(db) -> None:
    events, _, _ = await _wire(db)

    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(body="thanks, noted"), occurred_at=NOW
    )
    await events.wait_idle()

    stored = await events.get(result.event_id)
    assert stored is not None and stored.urgency == 0


@pytest.mark.integration
async def test_the_floor_holds_even_when_the_classifier_fails(db) -> None:
    events, _, _ = await _wire(db, reading=RuntimeError("provider down"), enrichment=True)

    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(body="URGENT: reply now"), occurred_at=NOW
    )
    await events.wait_idle()

    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.urgency == 2  # never unscored, just unsummarised
    assert stored.summary == ""


# ---------------------------------------------------------------------------
# the classifier, behind the flag
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_with_the_flag_off_the_classifier_never_runs(db) -> None:
    classifier = ScriptedClassifier({"urgency": 3, "summary": "should not appear"})
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    events = EventStore(db, cipher, audit, ContactsConfig())
    events.attach_enrichment(
        entities=Entities(db, cipher, audit, FakePersonJudge([])),
        commitments=Commitments(db, cipher, audit),
        enricher=Enricher(classifier),
        enrichment=False,
    )

    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW
    )
    await events.wait_idle()

    assert classifier.calls == []
    stored = await events.get(result.event_id)
    assert stored is not None and stored.summary == ""


@pytest.mark.integration
async def test_with_the_flag_on_the_reading_lands_in_the_sealed_meta(db) -> None:
    events, _, _ = await _wire(
        db,
        reading={
            "urgency": 3,
            "needs_reply": True,
            "action_type": "pay",
            "deadlines": ["by EOD"],
            "summary": "Rahul wants the October invoice paid",
            "noise": False,
        },
        enrichment=True,
    )

    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW
    )
    await events.wait_idle()

    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.urgency == 3
    assert stored.needs_reply is True
    assert stored.summary == "Rahul wants the October invoice paid"
    assert stored.deadline_at is not None
    assert stored.meta["action_type"] == "pay"

    # and it is sealed, not a plain column that leaked
    blob = await db.fetchval("SELECT meta_enc FROM events")
    assert "October invoice" not in blob.decode("utf-8", "replace")


@pytest.mark.integration
async def test_a_classified_commitment_is_written(db) -> None:
    events, _, commitments = await _wire(
        db,
        reading={
            "urgency": 2,
            "commitments": [
                {"kind": "request", "text": "send the invoice", "due": "by EOD"},
            ],
        },
        enrichment=True,
    )

    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW
    )
    await events.wait_idle()

    open_items = await commitments.list_open()
    assert [c.text for c in open_items] == ["send the invoice"]
    assert open_items[0].kind == "request"
    assert open_items[0].source_event_id == result.event_id
    assert open_items[0].due_at is not None


@pytest.mark.integration
async def test_a_noise_event_keeps_only_its_summary_chunk(db) -> None:
    events, _, _ = await _wire(
        db,
        reading={"urgency": 0, "noise": True, "summary": "a marketing newsletter"},
        enrichment=True,
        embedder=FakeEmbedder(),
    )

    result = await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(body="buy now " * 400),
        occurred_at=NOW,
    )
    await events.wait_idle()
    assert events._indexer is not None
    await events._indexer.wait_idle()

    rows = await db.fetch(
        "SELECT id, text_enc FROM memory_chunks WHERE event_id = $1", result.event_id
    )
    # one chunk, not twenty: a newsletter must not win slots from real mail
    assert len(rows) == 1
    assert "buy now" not in await events._indexer.read_document(result.event_id)


# ---------------------------------------------------------------------------
# entity linking and the owner rules
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_mails_addresses_are_linked_to_identities(db) -> None:
    events, entities, _ = await _wire(db)

    result = await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(to="me@example.com, Priya <priya@example.com>"),
        occurred_at=NOW,
    )
    await events.wait_idle()

    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.entity_ids  # sender and recipients all linked
    for entity_id in stored.entity_ids:
        assert await entities.get(entity_id) is not None
    rahul = await entities.resolve_exact("email", "rahul@example.com")
    assert rahul is not None and rahul.id in stored.entity_ids


@pytest.mark.integration
async def test_the_owner_address_is_known_and_rendered_as_such(db) -> None:
    _, entities, _ = await _wire(db)

    owner = await entities.mark_owner_address("gmail", "me@example.com", "Vedant")

    assert owner is not None and owner.owner is True
    assert await entities.is_owner_address("email", "me@example.com") is True
    assert await entities.is_owner_address("email", "rahul@example.com") is False


@pytest.mark.integration
async def test_an_alias_candidate_is_asked_once_and_never_merged(db) -> None:
    events, entities, _ = await _wire(db)
    await entities.mark_owner_address("gmail", "me@example.com", "Vedant Sharma")

    payload = _mail(sender="Vedant Sharma <vedant@work.com>")
    await events.ingest(source=MAIL_SOURCE, kind=MAIL_KIND, payload=payload, occurred_at=NOW)
    await events.wait_idle()

    # a new identity for the new address — never folded into the owner's
    candidate = await entities.resolve_exact("email", "vedant@work.com")
    owner = await entities.resolve_exact("email", "me@example.com")
    assert candidate is not None and owner is not None
    assert candidate.id != owner.id
    assert candidate.owner is False

    # asked once, and only once
    assert len(await entities.pending_alias_questions()) == 1
    await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(message_id="m2", sender="Vedant Sharma <vedant@work.com>"),
        occurred_at=NOW,
    )
    await events.wait_idle()
    assert len(await entities.pending_alias_questions()) == 1


@pytest.mark.integration
async def test_an_unrelated_sender_raises_no_question(db) -> None:
    events, entities, _ = await _wire(db)
    await entities.mark_owner_address("gmail", "me@example.com", "Vedant Sharma")

    await events.ingest(source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW)
    await events.wait_idle()

    assert await entities.pending_alias_questions() == []


@pytest.mark.integration
async def test_a_confirmed_alias_becomes_the_owners_address(db) -> None:
    events, entities, _ = await _wire(db)
    await entities.mark_owner_address("gmail", "me@example.com", "Vedant Sharma")
    await events.ingest(source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW)
    await events.wait_idle()
    candidate = await entities.resolve_exact("email", "rahul@example.com")
    assert candidate is not None

    # the user answers through the existing note_entity tool
    await entities.note(candidate.id, {"kind": "owner_alias", "note": "yes, that is me"})

    await entities.link_address("email", "rahul@example.com", "Rahul Sharma")
    refreshed = await entities.get(candidate.id)
    assert refreshed is not None and refreshed.owner is True
    # the question is answered, so it stops being asked
    assert await entities.pending_alias_questions() == []


@pytest.mark.integration
async def test_a_short_reply_tick_carries_the_alias_question(db) -> None:
    events, entities, _ = await _wire(db)
    await entities.mark_owner_address("gmail", "me@example.com", "Vedant Sharma")
    await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(sender="Vedant Sharma <vedant@work.com>"),
        occurred_at=NOW,
    )
    await events.wait_idle()

    from aether.authz.approvals import Approvals
    from aether.config import AgentConfig

    cipher = Cipher.from_b64(generate_key_b64())
    builder = ContextBuilder(events, entities, Approvals(db, cipher, AuditLog(db)), AgentConfig())

    asked = await builder.build(with_questions=True)
    silent = await builder.build(with_questions=False)

    assert len(asked.questions) == 1
    assert "vedant@work.com" in asked.questions[0]
    assert silent.questions == []


# ---------------------------------------------------------------------------
# two writers, one blob
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_salience_write_and_an_enrichment_write_both_survive(db) -> None:
    events, _, _ = await _wire(
        db,
        reading={"urgency": 2, "summary": "the invoice"},
        enrichment=True,
    )
    result = await events.ingest(
        source=MAIL_SOURCE, kind=MAIL_KIND, payload=_mail(), occurred_at=NOW
    )
    await events.wait_idle()

    # the judge lands after the enrichment — the classic lost-update race
    await events.update_salience(result.event_id, 8.0, True, category="work")

    stored = await events.get(result.event_id)
    assert stored is not None
    assert stored.salience_score == 8.0
    assert stored.memorable is True
    assert stored.meta["category"] == "work"  # the salience field
    assert stored.summary == "the invoice"  # and the enrichment field
    assert stored.urgency == 2


# ---------------------------------------------------------------------------
# facts at ingest
# ---------------------------------------------------------------------------


async def _wire_facts(db, reading: object):
    cipher = Cipher.from_b64(generate_key_b64())
    audit = AuditLog(db)
    entities = Entities(db, cipher, audit, FakePersonJudge([]))
    facts = Facts(db, cipher, audit)
    events = EventStore(db, cipher, audit, ContactsConfig())
    events.attach_enrichment(
        entities=entities,
        facts=facts,
        enricher=Enricher(ScriptedClassifier(reading)),
        enrichment=True,
    )
    return events, facts


@pytest.mark.integration
async def test_an_owned_fact_is_consolidated_at_ingest(db) -> None:
    events, facts = await _wire_facts(
        db,
        {
            "facts": [
                {
                    "subject": "user",
                    "key": "timezone",
                    "value": "Asia/Kolkata",
                    "type": "preference",
                }
            ]
        },
    )

    await events.ingest(
        source="web",
        kind="chat_message",
        payload={"text": "my timezone is IST", "chat_ref": "c1"},
        sender=Sender("web", "owner"),
    )
    await events.wait_idle()

    active = await facts.active()
    assert [(fact.subject, fact.key, fact.value) for fact in active] == [
        ("user", "timezone", "Asia/Kolkata")
    ]


@pytest.mark.integration
async def test_a_reserved_fact_from_mail_is_disputed_at_ingest(db) -> None:
    events, facts = await _wire_facts(
        db,
        {
            "facts": [
                {
                    "subject": "user",
                    "key": "email_address",
                    "value": "attacker@example.com",
                    "type": "contact",
                }
            ]
        },
    )

    await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload=_mail(sender="Rahul Sharma <rahul@example.com>"),
        sender=Sender("gmail", "rahul@example.com"),
        occurred_at=NOW,
    )
    await events.wait_idle()

    # an untrusted mail cannot establish a contact; it is held and asked once
    assert await facts.active() == []
    pending = await facts.pending_questions()
    assert len(pending) == 1
    assert pending[0].value == "attacker@example.com"
