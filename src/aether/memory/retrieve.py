"""Retrieval with complementary methods, fused and gated.

One question, many lists: lexical BM25 over chunks, the optional vector
signal, the entity timeline, and the structured views for questions that
ask about now. A weak single list is exactly the failure this phase
removes — a paraphrase only the embedder sees, a name only the keyword
index sees, a deadline only the attention view sees.

The lists are ranked independently and fused with reciprocal rank fusion
(k=60), then scored with the ranking signals plan.md section 5 names:
recency decay (mail 3 days, documents 30 days), entity match, urgency and
salience. Weights live in config, so the evaluation tunes them rather
than this file.

Selection is conservative: top 30, an optional listwise rerank (off until
the eval says it earns a model call), MMR (lambda 0.7) against duplicated
text, at most three chunks per event, neighbor chunks beside a document
hit, then the relevance gate. A gate that finds nothing returns an
explicit "no relevant memory" block — the model must say so instead of
inventing one.

Packing is the last step: a token budget (default 2,500), the owner's own
addresses so "(your other address)" stays truthful, structured urgent and
due lines when the route asked for them, then every chunk cited
`[E123 | mail | from Name <addr> | 2026-11-03 11:46 IST]`. Live results
carry "as of HH:MM". Everything here runs over in-memory, decrypted values.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .indexes import IndexedEvent, MemoryIndex
from .lexical import LexicalHit, LexicalIndex
from .mail import parse_addresses
from .query import Routing
from .summaries import DAILY, WEEKLY
from .views import AttentionItem, Commitment, Views

log = logging.getLogger("aether.memory.retrieve")

_CITATION_TIME_SECONDS = re.compile(r"\s+")


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class RetrievalConfig:
    """Every ranking knob in one place — config tunes, this file executes."""

    rrf_k: int = 60
    rank_pool: int = 30
    select_pool: int = 30
    mmr_lambda: float = 0.7
    w_recency: float = 0.35
    w_entity: float = 0.25
    w_urgency: float = 0.25
    w_salience: float = 0.15
    half_life_mail_days: float = 3.0
    half_life_doc_days: float = 30.0
    gate_min_score: float = 0.45
    # a vector-only hit may pass the gate at or above this cosine — a
    # genuine paraphrase has no shared term to match on, and the eval's
    # paraphrase subset is what decides whether this path stays
    gate_min_vector: float = 0.5
    pack_token_budget: int = 2_500
    max_chunks_per_event: int = 3
    max_lines: int = 8
    rerank_enabled: bool = False
    rerank_min_candidates: int = 12
    rerank_margin: float = 0.15


@dataclass
class Candidate:
    """One thing retrieval might cite, with the evidence behind its score."""

    event_id: int
    source: str
    kind: str
    occurred_at: datetime
    text: str
    chunk_id: int = 0
    ordinal: int = 0
    filename: str = ""
    location: str = ""
    rrf: float = 0.0
    score: float = 0.0
    entity_match: bool = False
    urgency: int = 0
    salience: float = 0.0
    neighbor: bool = False
    # the cosine this candidate came from, when the vector list contributed
    # it — the gate's paraphrase evidence (0.0 for lexical-only hits)
    vector_similarity: float = 0.0


@dataclass
class Packed:
    """What one routing produced for the prompt and the turn record."""

    block: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    # event id -> the exact packed lines for it, so the verifier checks a
    # quote against what the model was actually shown
    chunks_by_event: dict[int, list[str]] = field(default_factory=dict)
    gate: str = "ok"
    notes: list[str] = field(default_factory=list)


class Retriever:
    """Fuse, select, gate and pack; the pipeline's engine, one turn at a time."""

    def __init__(
        self,
        lexical: LexicalIndex,
        index: MemoryIndex,
        views: Views | None,
        vectors: Any = None,
        *,
        summaries: Any = None,
        cfg: RetrievalConfig | None = None,
        timezone: str = "Asia/Kolkata",
        reranker: Any = None,
    ) -> None:
        self._lexical = lexical
        self._index = index
        self._views = views
        self._vectors = vectors
        self._summaries = summaries
        self._cfg = cfg or RetrievalConfig()
        self._timezone = timezone
        # optional listwise rerank: (question, candidate texts) -> ordering
        # of indices. Off until the evaluation shows it earns the call.
        self._reranker = reranker

    async def retrieve(
        self,
        routing: Routing,
        *,
        query_embed: Any = None,
        now: datetime | None = None,
        exclude_event_ids: set[int] | None = None,
        owner_addresses: set[str] | None = None,
    ) -> Packed:
        """The whole query half of section 5 for one question."""
        now = now or datetime.now(UTC)
        excluded = exclude_event_ids or set()
        notes: list[str] = list(routing.notes)
        lists = await self._lists(routing, query_embed, now, notes)
        candidates = self._fuse(lists, routing, now, excluded)
        selected = await self._select(candidates, routing)
        packed = await self._pack(
            selected,
            routing,
            now,
            owner_addresses=owner_addresses or set(),
            notes=notes,
            candidates=candidates,
        )
        return packed

    # -- parallel retrieval ----------------------------------------------------

    async def _lists(
        self,
        routing: Routing,
        query_embed: Any,
        now: datetime,
        notes: list[str],
    ) -> list[list[Candidate]]:
        """Every retriever, each failure costing its own list and never the
        question. Vector degradation is announced, not silent."""
        lists: list[list[Candidate]] = []
        queries = routing.queries or (routing.text,)

        for query in queries[:3]:
            try:
                lexical_hits = self._lexical.search(
                    query,
                    limit=self._cfg.rank_pool,
                    source=routing.filters.source,
                    location=routing.filters.location,
                    filename_stem=routing.filters.doc_ref,
                )
            except Exception:
                log.exception("lexical search failed for one rewritten query")
                lexical_hits = []
            lists.append(
                [
                    self._candidate_from_lexical(hit)
                    for hit in lexical_hits
                    if self._in_window(hit.occurred_at, routing)
                ]
            )

        vector_list: list[Candidate] = []
        if self._vectors is None or not getattr(self._vectors, "enabled", False):
            notes.append("semantic search is unavailable — results are keyword-based")
        else:
            try:
                if query_embed is not None:
                    q = await query_embed
                else:
                    q = await self._vectors.embed_query(routing.text)
                if q is not None:
                    hits = await self._vectors.search_vector(q, limit=self._cfg.rank_pool)
                    vector_list = [
                        Candidate(
                            event_id=hit.event_id,
                            source=hit.source,
                            kind=hit.kind,
                            occurred_at=hit.occurred_at,
                            text=hit.text,
                            chunk_id=hit.chunk_id,
                            filename=hit.filename,
                            location=hit.location,
                            vector_similarity=float(hit.score),
                        )
                        for hit in hits
                        if self._in_window(hit.occurred_at, routing)
                    ]
                else:
                    notes.append("semantic search is unavailable — results are keyword-based")
            except Exception:
                log.exception("vector retrieval failed — lexical results still stand")
                notes.append("semantic search failed this turn — results are keyword-based")
        if vector_list:
            lists.append(vector_list)

        if "person" in routing.routes and self._views is not None:
            timeline = await self._timeline_candidates(routing, now)
            if timeline:
                lists.append(timeline)
        return lists

    async def _timeline_candidates(self, routing: Routing, now: datetime) -> list[Candidate]:
        """The person route's "everything with X", as a candidate list."""
        entities = routing.filters.entity_ids
        if not entities:
            return []
        out: list[Candidate] = []
        for entity_id in entities[:2]:
            try:
                events = await self._views.entity_timeline(entity_id, limit=12)  # type: ignore[union-attr]
            except Exception:
                log.exception("entity timeline failed for one identity")
                continue
            for event in events:
                out.append(self._candidate_from_event(event))
        return out[: self._cfg.rank_pool]

    # -- candidates and fusion -------------------------------------------------

    def _candidate_from_lexical(self, hit: LexicalHit) -> Candidate:
        event = self._index.get(hit.event_id)
        return Candidate(
            event_id=hit.event_id,
            source=hit.source,
            kind=hit.kind,
            occurred_at=hit.occurred_at,
            text=hit.text,
            chunk_id=hit.chunk_id,
            ordinal=hit.ordinal,
            filename=hit.filename,
            location=hit.location,
            urgency=event.urgency if event is not None else 0,
            salience=event.salience_score if event is not None else 0.0,
        )

    def _candidate_from_event(self, event: IndexedEvent) -> Candidate:
        text = event.summary or _event_words(event)
        return Candidate(
            event_id=event.id,
            source=event.source,
            kind=event.kind,
            occurred_at=event.occurred_at,
            text=text,
            urgency=event.urgency,
            salience=event.salience_score,
        )

    def _fuse(
        self,
        lists: list[list[Candidate]],
        routing: Routing,
        now: datetime,
        excluded: set[int],
    ) -> list[Candidate]:
        """RRF across the lists, then the section 5 score on top."""
        by_chunk: dict[tuple[int, int], Candidate] = {}
        for ranked in lists:
            seen_in_list: set[tuple[int, int]] = set()
            for rank, candidate in enumerate(ranked):
                if candidate.event_id in excluded:
                    continue
                key = (candidate.chunk_id, candidate.event_id)
                if key in seen_in_list:
                    continue
                seen_in_list.add(key)
                existing = by_chunk.get(key)
                if existing is None:
                    candidate.rrf = 1.0 / (self._cfg.rrf_k + rank + 1)
                    by_chunk[key] = candidate
                else:
                    existing.rrf += 1.0 / (self._cfg.rrf_k + rank + 1)

        for candidate in by_chunk.values():
            event = self._index.get(candidate.event_id)
            if event is not None:
                candidate.urgency = event.urgency
                candidate.salience = event.salience_score
                candidate.entity_match = bool(
                    routing.filters.entity_ids
                    and set(event.entity_ids) & set(routing.filters.entity_ids)
                )
            candidate.score = candidate.rrf + self._signals(candidate, now)
        ranked = sorted(
            by_chunk.values(),
            key=lambda candidate: (-candidate.score, -candidate.occurred_at.timestamp()),
        )
        return ranked

    def _signals(self, candidate: Candidate, now: datetime) -> float:
        """Recency decay, entity match, urgency and salience — weights from
        config, half-lives by kind (mail 3 days, documents 30)."""
        age_days = max(0.0, (now - candidate.occurred_at).total_seconds() / 86_400)
        if candidate.kind == "shared_doc":
            half_life = self._cfg.half_life_doc_days
        else:
            half_life = self._cfg.half_life_mail_days
        recency = 0.5 ** (age_days / max(0.1, half_life))
        return (
            self._cfg.w_recency * recency
            + self._cfg.w_entity * (1.0 if candidate.entity_match else 0.0)
            + self._cfg.w_urgency * (candidate.urgency / 3.0)
            + self._cfg.w_salience * (candidate.salience / 10.0)
        )

    def _in_window(self, when: datetime, routing: Routing) -> bool:
        time_from = routing.filters.time_from
        time_until = routing.filters.time_until
        if time_from is not None and when < time_from:
            return False
        return not (time_until is not None and when >= time_until)

    # -- selection ---------------------------------------------------------

    async def _select(self, candidates: list[Candidate], routing: Routing) -> list[Candidate]:
        """Top 30 -> optional rerank -> gate -> MMR -> per-event cap ->
        neighbor expansion."""
        if not candidates:
            return []
        pool = candidates[: self._cfg.select_pool]
        pool = await self._maybe_rerank(pool, routing)
        passing = [candidate for candidate in pool if self._passes_gate(candidate, pool, routing)]
        if not passing:
            return []
        selected = _mmr(
            passing,
            self._cfg.mmr_lambda,
            self._cfg.max_lines,
            per_event_cap=self._cfg.max_chunks_per_event,
        )
        selected = self._expand_neighbors(selected, routing)
        return selected[: self._cfg.max_lines]

    async def _maybe_rerank(self, candidates: list[Candidate], routing: Routing) -> list[Candidate]:
        """A listwise model rerank, only when it can change an ambiguous
        order: enabled in config, more than `rerank_min_candidates`, and a
        top-two score margin below `rerank_margin`. A bad or partial answer
        from the reranker leaves the fused order untouched."""
        if (
            not self._cfg.rerank_enabled
            or self._reranker is None
            or len(candidates) <= self._cfg.rerank_min_candidates
        ):
            return candidates
        top = candidates[0].score or 1.0
        second = candidates[1].score if len(candidates) > 1 else 0.0
        if (top - second) / top >= self._cfg.rerank_margin:
            return candidates
        try:
            order = await self._reranker(routing.text, [candidate.text for candidate in candidates])
        except Exception:
            log.warning("listwise rerank failed — keeping the fused order", exc_info=True)
            return candidates
        if not order or sorted(order) != list(range(len(candidates))):
            return candidates
        return [candidates[index] for index in order]

    def _passes_gate(self, candidate: Candidate, pool: list[Candidate], routing: Routing) -> bool:
        """A minimum normalized score and a real match: the fused score has
        to stand out in this turn's own pool, and the hit needs either an
        entity match or one of the question's own words. This is what turns
        "nothing relevant" into NO_RELEVANT_MEMORY instead of a guess."""
        top = max(candidate.score for candidate in pool) or 1.0
        if candidate.score / top < self._cfg.gate_min_score:
            return False
        if candidate.entity_match:
            return True
        if candidate.vector_similarity >= self._cfg.gate_min_vector:
            return True
        if routing.filters.doc_ref or routing.filters.location:
            return True
        return _has_query_term(candidate.text, routing.text)

    def _expand_neighbors(self, selected: list[Candidate], routing: Routing) -> list[Candidate]:
        """The chunks beside a document hit, so slide 3 can be read with
        slide 4. Only resident chunks — a bounded hot-set read, never a
        surprise cold scan."""
        out: list[Candidate] = []
        per_event: dict[int, int] = {}
        for candidate in selected:
            if per_event.get(candidate.event_id, 0) >= self._cfg.max_chunks_per_event:
                continue
            out.append(candidate)
            per_event[candidate.event_id] = per_event.get(candidate.event_id, 0) + 1
            if candidate.kind != "shared_doc" or candidate.neighbor:
                continue
            try:
                neighbors = self._lexical.neighbors(
                    LexicalHit(
                        event_id=candidate.event_id,
                        chunk_id=candidate.chunk_id,
                        ordinal=candidate.ordinal,
                        source=candidate.source,
                        kind=candidate.kind,
                        filename=candidate.filename,
                        location=candidate.location,
                        occurred_at=candidate.occurred_at,
                        text=candidate.text,
                        score=0.0,
                    )
                )
            except Exception:
                neighbors = []
            for neighbor in neighbors[:1]:
                if per_event.get(neighbor.event_id, 0) >= self._cfg.max_chunks_per_event:
                    break
                out.append(
                    Candidate(
                        event_id=neighbor.event_id,
                        source=neighbor.source,
                        kind=neighbor.kind,
                        occurred_at=neighbor.occurred_at,
                        text=neighbor.text,
                        chunk_id=neighbor.chunk_id,
                        ordinal=neighbor.ordinal,
                        filename=neighbor.filename,
                        location=neighbor.location,
                        score=candidate.score,
                        neighbor=True,
                    )
                )
                per_event[neighbor.event_id] = per_event.get(neighbor.event_id, 0) + 1
        return out

    # -- packing ---------------------------------------------------------

    async def _pack(
        self,
        selected: list[Candidate],
        routing: Routing,
        now: datetime,
        *,
        owner_addresses: set[str],
        notes: list[str],
        candidates: list[Candidate],
    ) -> Packed:
        """The prompt block: core facts, structured attention when routed,
        the selected chunks with their citations, inside a token budget."""
        budget_chars = self._cfg.pack_token_budget * 4
        zone = _zone(self._timezone)
        lines: list[str] = []
        trace: list[dict[str, Any]] = []
        event_ids: list[int] = []
        chunks_by_event: dict[int, list[str]] = {}
        used = 0
        structured = 0

        def add(line: str, entry: dict[str, Any] | None = None) -> bool:
            nonlocal used
            if used + len(line) > budget_chars:
                return False
            lines.append(line)
            used += len(line) + 1
            if entry is not None:
                trace.append(entry)
            return True

        if owner_addresses:
            add(
                "- these are also the user's own addresses; mail from them is "
                "from the user themselves: " + ", ".join(sorted(owner_addresses))
            )
        # degradation is never silent: a turn that lost its semantic half
        # says so in the working context, not only in a log line
        for note in dict.fromkeys(notes):
            add(f"- retrieval note: {note}")

        # the always-on facts block: standing rules and the claims this
        # question is about, inside its own character budget. A fact's
        # provenance is registered as the text's source, so a quote from the
        # brief is as checkable as a quote from a chunk.
        if self._views is not None and hasattr(self._views, "core_fact_items"):
            try:
                fact_items = await self._views.core_fact_items(
                    routing.text,
                    entity_ids=routing.filters.entity_ids,
                    now=now,
                )
            except Exception:
                fact_items = []
            if fact_items:
                add("- what you know about the user and their world:")
                for fact in fact_items:
                    line = _fact_brief_line(fact)
                    if not add(f"  {line}"):
                        continue
                    for event_id in fact.provenance:
                        chunks_by_event.setdefault(event_id, []).append(line)
                        if event_id not in event_ids:
                            event_ids.append(event_id)
                structured += 1

        # broad or time-windowed questions get the matching summary before
        # the raw chunks; its source ids ride along so a quote is checkable
        for line, source_ids in await self._summary_lines(routing, now):
            add(
                line,
                {
                    "id": source_ids[0] if source_ids else 0,
                    "source": "summary",
                    "kind": "summary",
                    "line": f"[related memory — {_summary_where(line)}] {_summary_body(line)}",
                },
            )
            for event_id in source_ids:
                chunks_by_event.setdefault(event_id, []).append(line)
            for event_id in source_ids:
                if event_id not in event_ids:
                    event_ids.append(event_id)
            structured += 1

        live = "attention" in routing.routes or "live_now" in routing.routes
        if live and self._views is not None:
            stamp = now.astimezone(zone).strftime("%H:%M")
            add(f"- attention (live, as of {stamp}):")
            structured += 1
            try:
                items = self._views.pending_attention(now=now, limit=5)
            except Exception:
                items = []
            if not items:
                add("  nothing urgent or due right now")
            for item in items:
                rendered = _attention_line(item, now, zone)
                added = add(
                    f"  {rendered}",
                    {
                        "id": item.event_id,
                        "source": item.source,
                        "kind": item.kind,
                        "line": f"[related memory — urgent item] {item.summary or 'an urgent event'}",
                    },
                )
                if added:
                    # the item's own line is citable and quotable like a
                    # chunk: without this a reply that says "the outage is
                    # urgent [E1]" would read as an invented id
                    chunks_by_event.setdefault(item.event_id, []).append(rendered)
                    if item.event_id not in event_ids:
                        event_ids.append(item.event_id)
                structured += 1

        if "commitment" in routing.routes and self._views is not None:
            try:
                open_items = await self._views.commitments_view(limit=5)
            except Exception:
                open_items = []
            if open_items:
                structured += 1
            for commitment in open_items:
                rendered = _commitment_line(commitment, now, zone)
                added = add(f"- {rendered}")
                # a promise's line is citable by the event it came from, so
                # "I promised X [E12]" is checked instead of flagged
                if added and commitment.source_event_id is not None:
                    chunks_by_event.setdefault(commitment.source_event_id, []).append(rendered)
                    if commitment.source_event_id not in event_ids:
                        event_ids.append(commitment.source_event_id)
                structured += 1

        for candidate in selected:
            citation = self._citation(candidate, zone)
            body = _clip(candidate.text, 600)
            line = f"{citation} {body}".strip()
            add(
                line,
                {
                    "id": candidate.event_id,
                    "source": candidate.source,
                    "kind": candidate.kind,
                    # the panel renders the backend's plain citation shape —
                    # "where: what" — never the raw packed line, so the E id
                    # never reaches a screen
                    "line": f"[related memory — {_plain_where(candidate, zone, self._index)}] {body[:200]}",
                },
            )
            chunks_by_event.setdefault(candidate.event_id, []).append(line)
            if candidate.event_id not in event_ids:
                event_ids.append(candidate.event_id)

        if not selected and structured == 0:
            # the gate found nothing that could support an answer. This is
            # the explicit abstain the model must repeat, in its own words.
            gate = "no_relevant_memory"
            add(
                "- no relevant memory: the records contain nothing that answers "
                "this question. Say plainly that you do not have it; do not "
                "guess or invent one."
            )
        else:
            gate = "ok"

        if selected:
            add(
                "- cite memory claims with the [E...] id from the packed line; "
                "quotes, names, numbers and dates must come from the cited text."
            )

        for note in notes:
            log.info("retrieval note: %s", note)
        return Packed(
            block="\n".join(lines),
            trace=trace,
            event_ids=event_ids,
            chunks_by_event=chunks_by_event,
            gate=gate,
            notes=list(dict.fromkeys(notes)),
        )

    async def _summary_lines(self, routing: Routing, now: datetime) -> list[tuple[str, list[int]]]:
        """The saved summaries a broad or time-windowed question should see.

        A summary is a synthesis of several events, not one of them, so its
        first source id is the citation the model can point at and every
        source id is registered as the text's provenance for the verifier.
        """
        if self._summaries is None or not _wants_summary(routing):
            return []
        kind = WEEKLY if _week_words(routing.text) else DAILY
        try:
            found = await self._summaries.recent(kind, limit=2)
        except Exception:
            log.warning("could not read summaries for the pack", exc_info=True)
            return []
        out: list[tuple[str, list[int]]] = []
        for summary in found:
            if not summary.text or not summary.source_event_ids:
                continue
            ids = ", ".join(f"E{event_id}" for event_id in summary.source_event_ids[:8])
            line = (
                f"[E{summary.source_event_ids[0]} | {summary.kind} summary | "
                f"{summary.period} | from {ids}] {_clip(summary.text, 600)}"
            )
            out.append((line, list(summary.source_event_ids)))
        return out

    def _citation(self, candidate: Candidate, zone: ZoneInfo) -> str:
        """`[E123 | mail | from Name <addr> | 2026-11-03 11:46 IST]` — every
        packed chunk says what it is, who wrote it and when, so a claim can
        point at it and the verifier can check it."""
        when = candidate.occurred_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        if candidate.kind == "shared_doc":
            where = ", ".join(part for part in (candidate.filename, candidate.location) if part)
            label = f"doc {where}" if where else "doc"
        else:
            event = self._index.get(candidate.event_id)
            who = _sender_label(event) if event is not None else ""
            label = candidate.kind or candidate.source or "memory"
            if who:
                label = f"{label} from {who}"
        return f"[E{candidate.event_id} | {label} | {when}]"


def _attention_line(item: AttentionItem, now: datetime, zone: ZoneInfo) -> str:
    label = f"urgent({item.urgency})" if item.urgency >= 2 else "noted"
    when = ""
    if item.deadline_at is not None:
        due = item.deadline_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        when = f", overdue since {due}" if item.deadline_at < now else f", due {due}"
    summary = item.summary or "(no summary)"
    return f"[E{item.event_id} | {label}{when}] {summary}"


def _plain_where(candidate: Candidate, zone: ZoneInfo, index: MemoryIndex) -> str:
    """The panel's "where" for one recalled chunk: the document and page,
    or who wrote it and when — the same shape the old recall used, never
    the packed citation with its E id."""
    when = candidate.occurred_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    if candidate.kind == "shared_doc":
        parts = [part for part in (candidate.filename, candidate.location, when) if part]
        return ", ".join(parts) or "a shared document"
    event = index.get(candidate.event_id)
    who = _sender_label(event) if event is not None else ""
    if who:
        return f"from {who}, {when}"
    return f"{candidate.source or 'memory'}, {when}"


def _commitment_line(commitment: Commitment, now: datetime, zone: ZoneInfo) -> str:
    when = ""
    if commitment.due_at is not None:
        due = commitment.due_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        when = f" — overdue since {due}" if commitment.due_at < now else f" — due {due}"
    return f"commitment {commitment.id}: {commitment.kind}: {commitment.text}{when}"


def _sender_label(event: IndexedEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    parsed = parse_addresses(payload.get("sender"))
    if parsed:
        first = parsed[0]
        return f"{first.name} <{first.address}>" if first.name else first.address
    sender = payload.get("_sender")
    if isinstance(sender, dict) and sender.get("handle"):
        return str(sender["handle"])
    return ""


def _event_words(event: IndexedEvent) -> str:
    from .events import event_text  # local: events imports the vector layer

    try:
        rendered = event_text(event)
    except Exception:
        rendered = ""
    return rendered[:600]


def _has_query_term(text: str, query: str) -> bool:
    from .lexical import tokenize

    wanted = set(tokenize(query))
    if not wanted:
        return False
    have = set(tokenize(text))
    return bool(wanted & have)


def _mmr(
    candidates: list[Candidate], lam: float, limit: int, *, per_event_cap: int = 3
) -> list[Candidate]:
    """Maximal marginal relevance over scores and text overlap.

    The fused score is normalized by the pool's best, so relevance and
    redundancy share one scale; lambda 0.7 keeps relevance ahead while
    three near-identical chunks from one newsletter stop crowding the set.
    """
    pool = list(candidates)
    top = max((candidate.score for candidate in pool), default=0.0) or 1.0
    picked: list[Candidate] = []
    per_event: dict[int, int] = {}
    while pool and len(picked) < limit:
        best_index = 0
        best_value = float("-inf")
        for index, candidate in enumerate(pool):
            if per_event.get(candidate.event_id, 0) >= per_event_cap:
                continue
            relevance = candidate.score / top
            redundancy = max((_jaccard(candidate.text, item.text) for item in picked), default=0.0)
            value = lam * relevance - (1.0 - lam) * redundancy
            if value > best_value:
                best_value = value
                best_index = index
        chosen = pool.pop(best_index)
        if per_event.get(chosen.event_id, 0) >= per_event_cap:
            continue
        picked.append(chosen)
        per_event[chosen.event_id] = per_event.get(chosen.event_id, 0) + 1
    return picked


def _jaccard(left: str, right: str) -> float:
    from .lexical import tokenize

    a = set(tokenize(left))
    b = set(tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clip(text: str, limit: int) -> str:
    clean = _CITATION_TIME_SECONDS.sub(" ", text or "").strip()
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


_SUMMARY_TEXT_RE = re.compile(
    r"\b(what happened|summary|summarise|summarize|recap|brief|overnight|"
    r"yesterday|this week|last week|past week)\b",
    re.IGNORECASE,
)


def _wants_summary(routing: Routing) -> bool:
    """A broad or explicitly time-windowed question is the summary's job."""
    if routing.filters.time_from is not None or routing.filters.time_until is not None:
        return True
    return bool(_SUMMARY_TEXT_RE.search(routing.text or ""))


def _week_words(text: str) -> bool:
    return bool(re.search(r"\b(week|weekly)\b", text or "", re.IGNORECASE))


def _summary_where(line: str) -> str:
    return "a saved summary"


def _summary_body(line: str) -> str:
    _, _, body = (line or "").partition("] ")
    return body


def _fact_brief_line(fact: Any) -> str:
    """One core fact in the same plain shape the brief renders."""
    who = (fact.subject or "").strip()
    if not who and fact.subject_identity_id is not None:
        who = "the user"
    if fact.type == "rule":
        return f"rule: {fact.value}"
    if who:
        return f"{who} — {fact.key}: {fact.value}"
    return f"{fact.key}: {fact.value}"
