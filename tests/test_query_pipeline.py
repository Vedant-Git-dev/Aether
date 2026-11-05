"""The query pipeline's pure halves: lexical BM25, routing and references,
fusion/gating/packing, and the citation verifier.

Everything here is in-memory — no database, no network — because these are
the components every question runs through and their behaviour must be
readable in one place. The loop integration and the golden set live in
test_query_pipeline_loop.py and test_eval_golden.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aether.memory.focus import FocusState
from aether.memory.indexes import IndexedEvent, MemoryIndex
from aether.memory.lexical import LexicalIndex, tokenize
from aether.memory.query import RewriteCache, Routing, classify, resolve_reference, rewrite
from aether.memory.retrieve import Candidate, RetrievalConfig, Retriever
from aether.memory.verify import repair_reply, verify_reply
from aether.memory.views import Views

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _chunk(
    index: LexicalIndex,
    chunk_id: int,
    event_id: int,
    text: str,
    *,
    source: str = "gmail",
    kind: str = "mail",
    filename: str = "",
    location: str = "",
    ordinal: int = 0,
    when: datetime | None = None,
) -> None:
    index.add_chunk(
        chunk_id=chunk_id,
        event_id=event_id,
        ordinal=ordinal,
        source=source,
        kind=kind,
        filename=filename,
        location=location,
        occurred_at=when or NOW,
        text=text,
    )


def _event(
    event_id: int,
    *,
    kind: str = "mail",
    source: str = "gmail",
    occurred_at: datetime | None = None,
    urgency: int = 0,
    summary: str = "",
    entities: tuple[int, ...] = (),
    salience: float = 0.0,
    payload: dict | None = None,
) -> IndexedEvent:
    return IndexedEvent(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=occurred_at or NOW,
        salience_score=salience,
        memorable=False,
        payload=payload or {},
        meta={},
        urgency=urgency,
        summary=summary,
        entity_ids=entities,
    )


# ---------------------------------------------------------------------------
# lexical BM25
# ---------------------------------------------------------------------------


def test_tokenizer_folds_case_diacritics_and_plurals() -> None:
    assert "invoic" not in tokenize("invoices")
    assert tokenize("Invoices") == tokenize("invoices")
    assert tokenize("Résumé") == tokenize("resume")
    assert tokenize("the and of") == []  # stop words only
    assert "urgent" in tokenize("Kuch urgent hai?")  # hindi particles stop, the topic stays


def test_subject_and_filename_outrank_the_body() -> None:
    index = LexicalIndex()
    _chunk(
        index,
        1,
        1,
        "[Mail: Invoice for October | from Rahul <r@example.com> | 2026-11-03] "
        "Please find the details attached.",
    )
    _chunk(
        index,
        2,
        2,
        "[Mail: Weekend plans | from Amit <a@example.com> | 2026-11-03] "
        "No invoice talk here, just lunch.",
    )

    hits = index.search("invoice")
    assert [hit.event_id for hit in hits] == [1, 2]
    assert hits[0].score > hits[1].score


def test_typo_tolerance_matches_the_vocabulary() -> None:
    index = LexicalIndex()
    _chunk(index, 1, 1, "The invoice is attached and ready.")

    hits = index.search("invoce")

    assert hits and hits[0].event_id == 1


def test_exact_phrase_beats_scattered_words() -> None:
    index = LexicalIndex()
    _chunk(index, 1, 1, "The quarterly invoice is waiting for payment review on Friday.")
    _chunk(index, 2, 2, "The invoice due reminder sits at the top of the list.")

    hits = index.search('"invoice due"')

    assert hits[0].event_id == 2


def test_filters_narrow_by_source_location_and_filename() -> None:
    index = LexicalIndex()
    _chunk(
        index,
        1,
        1,
        "[Doc: OSY.pdf | slide 3 | 2026-11-03] Deadlock prevention needs an order.",
        kind="shared_doc",
        filename="OSY.pdf",
        location="slide 3",
    )
    _chunk(
        index,
        2,
        2,
        "[Doc: DBMS.pdf | page 2 | 2026-11-03] Deadlock prevention again.",
        kind="shared_doc",
        filename="DBMS.pdf",
        location="page 2",
    )

    assert [hit.event_id for hit in index.search("deadlock", location="slide 3")] == [1]
    assert [hit.event_id for hit in index.search("deadlock", filename_stem="OSY")] == [1]
    assert {hit.event_id for hit in index.search("deadlock", kind="shared_doc")} == {1, 2}


def test_the_hot_budget_trims_the_oldest_chunks() -> None:
    index = LexicalIndex(hot_chunks=16)
    for chunk_id in range(1, 30):
        _chunk(index, chunk_id, chunk_id, f"message number {chunk_id}")

    assert len(index) == 16
    found = {hit.event_id for hit in index.search("number")}
    assert found and min(found) >= 14  # the oldest chunks left the hot set


def test_removing_an_event_forgets_its_chunks() -> None:
    index = LexicalIndex()
    _chunk(index, 1, 7, "the invoice is attached")
    _chunk(index, 2, 7, "second invoice page")

    index.remove_event(7)

    assert index.search("invoice") == []


# ---------------------------------------------------------------------------
# routing and reference resolution
# ---------------------------------------------------------------------------


def _focus(**fields) -> FocusState:
    return FocusState(**fields)


def test_a_greeting_retrieves_nothing() -> None:
    assert classify("hi there").routes == ("chat",)


def test_attention_words_route_to_the_attention_view() -> None:
    routing = classify("kuch urgent hai?", now=NOW)

    assert "attention" in routing.routes
    assert routing.filters.urgency_min == 1
    # the trigger words alone leave nothing to search for
    assert "open_recall" not in routing.routes


def test_commitment_questions_route_to_the_commitments_view() -> None:
    routing = classify("what did I promise Rahul about the deck?", now=NOW)

    assert "commitment" in routing.routes


def test_a_known_name_routes_to_the_person_view() -> None:
    routing = classify(
        "what did Rahul say about the review?",
        entity_names={7: "Rahul"},
        now=NOW,
    )

    assert "person" in routing.routes
    assert routing.filters.entity_ids == (7,)


def test_slide_questions_carry_a_location_filter() -> None:
    routing = classify("explain slide 3 of OSY.pdf", now=NOW)

    assert "doc" in routing.routes
    assert routing.filters.location == "slide 3"
    assert routing.filters.doc_ref == "OSY.pdf"


def test_mail_questions_filter_to_mail() -> None:
    assert classify("what emails mention the invoice?", now=NOW).filters.source == "mail"


def test_time_words_become_a_window() -> None:
    routing = classify("what happened yesterday?", now=NOW)

    assert routing.filters.time_from == datetime(2026, 11, 2, tzinfo=UTC)
    assert routing.filters.time_until == datetime(2026, 11, 3, tzinfo=UTC)


def test_send_it_resolves_to_the_last_attachment() -> None:
    focus = _focus(last_attachment={"filename": "assignment.pdf", "event_id": 5, "media_id": 9})

    resolved, ask, _ = resolve_reference("send it", focus)

    assert ask is None
    assert resolved is not None
    assert resolved.kind == "attachment"
    assert resolved.event_id == 5
    assert resolved.media_id == 9


def test_send_then_resolves_the_same_way() -> None:
    focus = _focus(open_task={"commitment_id": 1, "summary": "send the assignment"})

    resolved, _, _ = resolve_reference("send then", focus)

    assert resolved is not None
    assert resolved.kind == "task"


def test_the_sender_resolves_to_the_focus_person() -> None:
    focus = _focus(recent_entities=[3])

    resolved, _, _ = resolve_reference("send it to the sender", focus, entity_names={3: "Rahul"})

    assert resolved is not None
    assert resolved.kind == "entity"
    assert resolved.entity_id == 3


def test_the_assignment_matches_the_open_task_by_name() -> None:
    focus = _focus(open_task={"commitment_id": 2, "summary": "finish the assignment today"})

    resolved, _, _ = resolve_reference("send the assignment then", focus)

    assert resolved is not None
    assert resolved.kind == "task"


def test_an_unresolvable_reference_asks_one_specific_question() -> None:
    focus = _focus(last_offer={"text": "Which one?", "options": ["the report", "the invoice"]})

    resolved, ask, _ = resolve_reference("send it", focus)

    assert resolved is None
    assert ask is not None
    assert "the report" in ask and "the invoice" in ask


def test_a_resolved_reference_skips_search() -> None:
    focus = _focus(last_attachment={"filename": "a.pdf", "event_id": 5})
    routing = classify("send it", focus=focus, now=NOW)

    assert routing.routes == ("reference",)
    assert routing.needs_search is False


def test_rewrite_expands_a_short_anaphoric_question_and_caches_it() -> None:
    focus = _focus(recent_entities=[3], open_task={"summary": "send the assignment"})
    cache = RewriteCache()
    routing = classify("what about that?", focus=focus, entity_names={3: "Rahul"}, now=NOW)
    routing = rewrite(routing, focus, entity_names={3: "Rahul"}, cache=cache)

    assert "Rahul" in " ".join(routing.queries)
    assert len(routing.queries) <= 4
    cached = rewrite(routing, focus, entity_names={3: "Rahul"}, cache=cache)
    assert cached.queries == routing.queries


def test_rewrite_leaves_a_full_question_alone() -> None:
    routing = classify("explain the invoice payment process for October", now=NOW)
    routing = rewrite(routing, None)

    assert routing.queries == ("explain the invoice payment process for October",)


# ---------------------------------------------------------------------------
# fusion, gate and pack
# ---------------------------------------------------------------------------


def _retriever(
    chunks: list[tuple[int, int, str]],
    events: list[IndexedEvent],
    *,
    cfg: RetrievalConfig | None = None,
) -> Retriever:
    lexical = LexicalIndex()
    for chunk_id, event_id, text in chunks:
        _chunk(lexical, chunk_id, event_id, text)
    index = MemoryIndex()
    for event in events:
        index.upsert(event)
    views = Views(index, timezone="Asia/Kolkata")
    return Retriever(lexical, index, views, vectors=None, cfg=cfg or RetrievalConfig())


async def test_retrieval_packs_cited_chunks_with_the_question_terms() -> None:
    retriever = _retriever(
        [
            (
                1,
                1,
                "[Mail: Invoice | from Rahul <r@example.com> | 2026-11-03] "
                "The October invoice is due on Friday.",
            )
        ],
        [_event(1)],
    )
    routing = classify("what about the invoice?", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert packed.gate == "ok"
    assert "[E1 |" in packed.block
    assert "invoice" in packed.block
    assert packed.event_ids == [1]


async def test_nothing_relevant_abstains_explicitly() -> None:
    retriever = _retriever(
        [(1, 1, "The invoice is due on Friday.")],
        [_event(1)],
    )
    routing = classify("what did the board decide about quantum computing?", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert packed.gate == "no_relevant_memory"
    assert "no relevant memory" in packed.block.lower()
    assert packed.event_ids == []


async def test_degraded_vector_is_stated_not_silent() -> None:
    retriever = _retriever([(1, 1, "The invoice is due on Friday.")], [_event(1)])
    routing = classify("invoice status", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert any("semantic search" in note for note in packed.notes)
    assert "retrieval note" in packed.block


async def test_attention_items_are_rendered_live_with_a_time_stamp() -> None:
    retriever = _retriever(
        [],
        [
            _event(
                1,
                urgency=3,
                summary="send the contract today",
                payload={},
            )
        ],
    )
    routing = classify("anything urgent?", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert "attention (live, as of" in packed.block
    assert "urgent(3)" in packed.block
    assert "[E1" in packed.block


async def test_the_pack_never_exceeds_its_budget() -> None:
    chunks = [
        (chunk_id, chunk_id, f"invoice line {chunk_id} " + ("word " * 200))
        for chunk_id in range(1, 12)
    ]
    retriever = _retriever(
        chunks,
        [_event(chunk_id) for chunk_id in range(1, 12)],
        cfg=RetrievalConfig(pack_token_budget=120, max_lines=8),
    )
    routing = classify("invoice", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert len(packed.block) <= 120 * 4


async def test_the_optional_rerank_reorders_only_an_ambiguous_pool() -> None:
    index = MemoryIndex()
    views = Views(index, timezone="Asia/Kolkata")

    async def reverse(query: str, texts: list[str]) -> list[int]:
        return list(reversed(range(len(texts))))

    retriever = Retriever(
        LexicalIndex(),
        index,
        views,
        None,
        cfg=RetrievalConfig(rerank_enabled=True, rerank_min_candidates=1, rerank_margin=1.0),
        reranker=reverse,
    )
    candidates = [
        Candidate(
            event_id=index_id,
            source="gmail",
            kind="mail",
            occurred_at=NOW,
            text=f"candidate {index_id}",
            score=1.0 - index_id * 0.01,
        )
        for index_id in range(3)
    ]
    routing = Routing(text="invoice", routes=("open_recall",))

    reordered = await retriever._maybe_rerank(candidates, routing)

    assert [candidate.event_id for candidate in reordered] == [2, 1, 0]

    # a reranker that answers with a partial or duplicate ordering is ignored
    async def broken(query: str, texts: list[str]) -> list[int]:
        return [0, 0]

    retriever._reranker = broken
    kept = await retriever._maybe_rerank(candidates, routing)
    assert [candidate.event_id for candidate in kept] == [0, 1, 2]


async def test_at_most_three_chunks_per_event_are_packed() -> None:
    chunks = [
        (ordinal, 1, f"[Doc: notes.pdf | page {ordinal}] invoice page {ordinal}")
        for ordinal in range(1, 6)
    ]
    retriever = _retriever(chunks, [_event(1, kind="shared_doc")])
    routing = classify("invoice page", now=NOW)
    routing = rewrite(routing, None)

    packed = await retriever.retrieve(routing, now=NOW)

    assert packed.block.count("[E1") <= 4  # three scored plus at most one neighbor
    assert len(packed.chunks_by_event.get(1, [])) <= 4


# ---------------------------------------------------------------------------
# verifier
# ---------------------------------------------------------------------------


def test_a_supported_reply_passes() -> None:
    chunks = {
        1: [
            "[E1 | mail | from Rahul <r@example.com> | 2026-11-03 11:46] "
            "The invoice is due on 2026-11-07."
        ]
    }
    reply = 'Rahul wrote "the invoice is due on 2026-11-07" [E1].'

    assert verify_reply(reply, chunks_by_event=chunks, known_event_ids={1}) == []


def test_an_invented_id_is_caught() -> None:
    violations = verify_reply(
        "As we agreed [E9].",
        chunks_by_event={1: ["[E1 | mail] hello"]},
        known_event_ids={1},
    )

    assert [v.kind for v in violations] == ["id"]


def test_a_fabricated_quote_is_caught_and_repaired() -> None:
    violations = verify_reply(
        'They said "the payment cleared yesterday".',
        chunks_by_event={1: ["[E1 | mail] The invoice is due on Friday."]},
        known_event_ids={1},
    )

    assert [v.kind for v in violations] == ["quote"]
    repaired = repair_reply('They said "the payment cleared yesterday".', violations)
    assert "the payment cleared yesterday" not in repaired.text
    assert "not verbatim" in repaired.text


def test_a_date_is_checked_across_spellings() -> None:
    chunks = {1: ["[E1 | mail] The deadline is 2026-11-07."]}

    assert (
        verify_reply("It is due 7 Nov 2026 [E1].", chunks_by_event=chunks, known_event_ids={1})
        == []
    )
    assert verify_reply("It is due 2026-11-09 [E1].", chunks_by_event=chunks, known_event_ids={1})


def test_quoting_the_users_own_words_is_not_a_memory_claim() -> None:
    reply = 'You asked me to "send the assignment today" [E1].'
    chunks = {1: ["[E1 | mail] The invoice is due on Friday."]}

    assert verify_reply(reply, chunks_by_event=chunks, known_event_ids={1})
    assert (
        verify_reply(
            reply,
            chunks_by_event=chunks,
            known_event_ids={1},
            extra_corpus="please send the assignment today",
        )
        == []
    )


def test_an_unsourced_number_or_date_gets_one_hedge() -> None:
    violations = verify_reply(
        "The invoice is 45000 and due 2026-12-01.",
        chunks_by_event={1: ["[E1 | mail] The invoice is due on Friday."]},
        known_event_ids={1},
    )

    assert any(v.kind == "number" for v in violations)
    assert any(v.kind == "date" for v in violations)
    repaired = repair_reply("The invoice is 45000 and due 2026-12-01.", violations)
    assert "could not be checked" in repaired.text
