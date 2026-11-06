"""One object the agent asks for memory.

The loop and the `memory_search` tool both come through here, so routing,
rewriting, retrieval, gating and packing happen in one place and the old
vector recall remains the fallback when `memory.query_pipeline` is off.
The pipeline is deliberately thin: the router decides what the question
is, the retriever finds and packs the evidence, and the verifier (used by
the loop after the answer) checks the claims the packed text must support.

Freshness: a live-state question reads the resident index, which every
poll and ingest keeps current, so the attention block is live by
construction and says "as of HH:MM". The conclusions cache is written
where it always was; the read side is the seam a live connector query
(Phase 3) will gate on, and this phase never serves an expired answer from
it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .focus import FocusState
from .indexes import MemoryIndex
from .lexical import LexicalIndex
from .query import RewriteCache, Routing, classify, rewrite
from .retrieve import Packed, RetrievalConfig, Retriever
from .views import Views

log = logging.getLogger("aether.memory.pipeline")


@dataclass
class PipelineResult:
    """The routing that ran and what it packed."""

    routing: Routing
    packed: Packed

    @property
    def block(self) -> str:
        return self.packed.block

    @property
    def needs_abstain(self) -> bool:
        return self.packed.gate == "no_relevant_memory"


def retrieval_config_from(memory: Any) -> RetrievalConfig:
    """Map `memory` config onto the retriever's knobs — one cast in one
    place, so a renamed field is a type error here and not a silent
    default deep in retrieval."""
    return RetrievalConfig(
        rrf_k=int(getattr(memory, "rrf_k", 60)),
        rank_pool=int(getattr(memory, "retrieve_pool", 30)),
        select_pool=int(getattr(memory, "select_pool", 30)),
        mmr_lambda=float(getattr(memory, "mmr_lambda", 0.7)),
        w_recency=float(getattr(memory, "w_recency", 0.35)),
        w_entity=float(getattr(memory, "w_entity", 0.25)),
        w_urgency=float(getattr(memory, "w_urgency", 0.25)),
        w_salience=float(getattr(memory, "w_salience", 0.15)),
        half_life_mail_days=float(getattr(memory, "half_life_mail_days", 3.0)),
        half_life_doc_days=float(getattr(memory, "half_life_doc_days", 30.0)),
        gate_min_score=float(getattr(memory, "relevance_floor", 0.45)),
        gate_min_vector=float(getattr(memory, "relevance_vector_floor", 0.5)),
        pack_token_budget=int(getattr(memory, "pack_token_budget", 2_500)),
        max_chunks_per_event=int(getattr(memory, "max_chunks_per_event", 3)),
        max_lines=int(getattr(memory, "max_packed_lines", 8)),
        rerank_enabled=bool(getattr(memory, "query_rerank", False)),
        rerank_margin=float(getattr(memory, "rerank_margin", 0.15)),
    )


class QueryPipeline:
    """Route one question, retrieve in parallel, fuse, gate, pack."""

    def __init__(
        self,
        *,
        lexical: LexicalIndex,
        index: MemoryIndex,
        views: Views | None,
        vectors: Any = None,
        summaries: Any = None,
        cfg: RetrievalConfig | None = None,
        timezone: str = "Asia/Kolkata",
        reranker: Any = None,
    ) -> None:
        self._rewrite_cache = RewriteCache()
        self._retriever = Retriever(
            lexical,
            index,
            views,
            vectors,
            summaries=summaries,
            cfg=cfg or RetrievalConfig(),
            timezone=timezone,
            reranker=reranker,
        )

    async def run(
        self,
        text: str,
        *,
        now: datetime | None = None,
        focus: FocusState | None = None,
        entity_names: dict[int, str] | None = None,
        query_embed: Any = None,
        exclude_event_ids: set[int] | None = None,
        owner_addresses: set[str] | None = None,
        force_attention: bool = False,
    ) -> PipelineResult:
        """The whole query half of section 5 for one question."""
        now = now or datetime.now(UTC)
        routing = classify(text, focus=focus, entity_names=entity_names, now=now)
        if force_attention and "attention" not in routing.routes:
            # an unprompted tick whose observations carry urgency: the
            # structured view is the proactive path's own retrieval
            routing = replace(
                routing,
                routes=("attention",) + routing.routes,
                filters=replace(routing.filters, urgency_min=max(1, routing.filters.urgency_min)),
            )
        routing = rewrite(routing, focus, entity_names=entity_names, cache=self._rewrite_cache)

        if not routing.routes or routing.routes == ("chat",):
            return PipelineResult(routing=routing, packed=Packed(gate="skip"))
        if routing.routes == ("reference",) and routing.resolved is not None:
            # a resolved reference needs no search: point the model at the
            # concrete id so "send it" cannot become a query
            resolved = routing.resolved
            target = f"- the user means {resolved.label}"
            if resolved.event_id is not None:
                target += f" (event E{resolved.event_id})"
            if resolved.media_id is not None:
                target += f" (media {resolved.media_id})"
            if resolved.entity_id is not None:
                target += f" (person {resolved.entity_id})"
            target += ". Act on that id directly; do not search for it."
            return PipelineResult(
                routing=routing,
                packed=Packed(block=target, gate="resolved", notes=list(routing.notes)),
            )
        if routing.ask is not None:
            return PipelineResult(
                routing=routing,
                packed=Packed(
                    block=f"- ask the user exactly this before acting: {routing.ask}",
                    gate="ask",
                    notes=list(routing.notes),
                ),
            )

        packed = await self._retriever.retrieve(
            routing,
            query_embed=query_embed,
            now=now,
            exclude_event_ids=exclude_event_ids,
            owner_addresses=owner_addresses,
        )
        return PipelineResult(routing=routing, packed=packed)

    async def search_text(
        self,
        query: str,
        *,
        now: datetime | None = None,
        focus: FocusState | None = None,
        entity_names: dict[int, str] | None = None,
        owner_addresses: set[str] | None = None,
    ) -> str:
        """The `memory_search` tool's answer: the same pipeline, in the
        tool's plain words, with the abstain stated instead of guessed."""
        result = await self.run(
            query,
            now=now,
            focus=focus,
            entity_names=entity_names,
            owner_addresses=owner_addresses,
        )
        if result.packed.gate == "no_relevant_memory":
            return "No matching memories."
        if result.packed.gate in ("skip", "resolved", "ask"):
            return result.packed.block or "No matching memories."
        return result.packed.block
