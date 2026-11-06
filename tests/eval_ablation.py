"""The two-arm evaluation runner (plan.md section 11).

The golden set answers "does retrieval still work?". This module answers the
decision the plan actually asks for: run the same cases with the vector path
off and on, split the numbers by question shape, and let the difference
justify keeping embeddings — or not.

Everything runs offline and in process over the real router, lexical index,
fusion, gate and packer. The vector arm is the fake embedder: a genuine
paraphrase gets its canned hit, every other case gets an enabled but empty
vector list, which is exactly what a real embedding miss looks like. Latency
and memory are measured here too, because section 11 lists them as metrics,
with a loose gate that only catches a real regression.
"""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC

from eval_golden import NOW, FakeCommitments, GoldenCase

from aether.memory.enrich import normalize_deadline
from aether.memory.indexes import MemoryIndex
from aether.memory.lexical import LexicalIndex
from aether.memory.pipeline import QueryPipeline
from aether.memory.retrieve import RetrievalConfig
from aether.memory.verify import Violation, verify_reply
from aether.memory.views import AttentionItem, Views

TIMEZONE = "Asia/Kolkata"
# the two arms, in the order the decision reads them
ARMS = ("lexical", "vector")


@dataclass
class Outcome:
    """One case's run: what it routed, packed, cited and cost."""

    case: GoldenCase
    routes: tuple[str, ...]
    gate: str
    cited: list[int]
    resolved_kind: str
    block: str
    ask: str
    chunks_by_event: dict[int, list[str]]
    known_ids: set[int]
    latency_ms: float = 0.0
    violations: tuple[Violation, ...] = ()

    @property
    def supported(self) -> bool:
        """True when the case's honest answer is fully backed by the pack."""
        return not self.violations


def build_pipeline(case: GoldenCase, *, vectors: bool | None) -> QueryPipeline:
    """The real pipeline over one case's tiny world.

    `vectors` is the arm: False keeps the path off, True turns it on for
    every case, and None is the canonical gate — on exactly where the case
    seeds a hit, which is what the golden set has always meant.
    """
    lexical = LexicalIndex()
    for chunk in case.chunks:
        lexical.add_chunk(
            chunk_id=chunk.chunk_id,
            event_id=chunk.event_id,
            ordinal=chunk.ordinal,
            source=chunk.source,
            kind=chunk.kind,
            filename=chunk.filename,
            location=chunk.location,
            occurred_at=NOW,
            text=chunk.text,
        )
    index = MemoryIndex()
    for event in case.events:
        index.upsert(event)
    commitments = FakeCommitments(case.commitments) if case.commitments else None
    vector_memory = None
    if vectors or (vectors is None and case.vector_hits):
        from fakes import FakeVectorMemory

        vector_memory = FakeVectorMemory(hits=list(case.vector_hits))
    return QueryPipeline(
        lexical=lexical,
        index=index,
        views=Views(index, commitments, timezone=TIMEZONE),
        vectors=vector_memory,
        cfg=RetrievalConfig(),
        timezone=TIMEZONE,
    )


async def run_case(
    case: GoldenCase, *, vectors: bool | None = False, repeats: int = 1, now=NOW
) -> Outcome:
    """Run one case, timing every repetition and keeping the last result."""
    pipeline = build_pipeline(case, vectors=vectors)
    result = None
    samples: list[float] = []
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        result = await pipeline.run(
            case.question,
            now=now,
            focus=case.focus,
            entity_names=case.entity_names,
            owner_addresses=case.owner_addresses,
        )
        samples.append((time.perf_counter() - started) * 1000.0)
    assert result is not None
    packed = result.packed
    cited = [int(entry["id"]) for entry in packed.trace if "id" in entry]
    known_ids = {int(event_id) for event_id in packed.event_ids}
    outcome = Outcome(
        case=case,
        routes=result.routing.routes,
        gate=packed.gate,
        cited=cited,
        resolved_kind=result.routing.resolved.kind if result.routing.resolved else "",
        block=packed.block,
        ask=result.routing.ask or "",
        chunks_by_event={int(k): list(v) for k, v in packed.chunks_by_event.items()},
        known_ids=known_ids,
        latency_ms=sum(samples) / len(samples),
    )
    if case.answer:
        outcome.violations = tuple(
            verify_reply(
                case.answer,
                chunks_by_event=outcome.chunks_by_event,
                known_event_ids=outcome.known_ids,
            )
        )
    return outcome


async def run_arm(
    cases: list[GoldenCase],
    *,
    vectors: bool | None = False,
    repeats: int = 1,
    now=NOW,
) -> list[Outcome]:
    return [await run_case(case, vectors=vectors, repeats=repeats, now=now) for case in cases]


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _metric_block(outcomes: list[Outcome]) -> dict[str, float]:
    routed = [
        set(outcome.case.expected_routes) <= set(outcome.routes)
        for outcome in outcomes
        if outcome.case.expected_routes
    ]
    recall: list[float] = []
    reciprocal: list[float] = []
    for outcome in outcomes:
        expected = outcome.case.expected_cited
        if not expected:
            continue
        hits = [event_id for event_id in expected if event_id in outcome.cited]
        recall.append(len(hits) / len(expected))
        if hits:
            first = min(outcome.cited.index(event_id) for event_id in hits)
            reciprocal.append(1.0 / (first + 1))
        else:
            reciprocal.append(0.0)
    abstain_true = [outcome for outcome in outcomes if outcome.case.expect_abstain]
    answerable = [outcome for outcome in outcomes if outcome.case.expected_cited]
    answered = [outcome for outcome in outcomes if outcome.case.answer]
    return {
        "recall@5": sum(recall) / len(recall) if recall else 1.0,
        "mrr": sum(reciprocal) / len(reciprocal) if reciprocal else 1.0,
        "route_accuracy": sum(routed) / len(routed) if routed else 1.0,
        "abstain_recall": (
            sum(1 for outcome in abstain_true if outcome.gate == "no_relevant_memory")
            / len(abstain_true)
            if abstain_true
            else 1.0
        ),
        # precision here is "did not wrongly abstain on answerable cases"
        "abstain_precision": (
            sum(1 for outcome in answerable if outcome.gate != "no_relevant_memory")
            / len(answerable)
            if answerable
            else 1.0
        ),
        "claim_support": (
            sum(1 for outcome in answered if outcome.supported) / len(answered) if answered else 1.0
        ),
    }


def metrics(outcomes: list[Outcome]) -> dict[str, dict[str, float]]:
    """Overall numbers plus one block per `subset` tag."""
    subsets: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        subsets.setdefault(outcome.case.subset or "other", []).append(outcome)
    return {
        "overall": _metric_block(outcomes),
        **{name: _metric_block(rows) for name, rows in sorted(subsets.items())},
    }


def failure_lines(outcomes: list[Outcome]) -> list[str]:
    """Every case whose shape the pipeline got wrong, with its own evidence."""
    return [
        f"{outcome.case.name}: routes={outcome.routes} gate={outcome.gate} "
        f"cited={outcome.cited} resolved={outcome.resolved_kind}"
        for outcome in outcomes
        if not outcome_ok(outcome)
    ]


def outcome_ok(outcome: Outcome) -> bool:
    """The per-case contract: route, reference resolution, abstain, citations,
    a named ambiguity, and the plain strings the pack must (not) carry."""
    case = outcome.case
    if case.expected_routes and not set(case.expected_routes) <= set(outcome.routes):
        return False
    if case.expected_ask_terms:
        return outcome.gate == "ask" and all(
            term in outcome.block for term in case.expected_ask_terms
        )
    if case.expected_resolved:
        return outcome.gate == "resolved" and outcome.resolved_kind == case.expected_resolved
    if case.expect_abstain:
        return outcome.gate == "no_relevant_memory"
    if case.expected_block_terms and not all(
        term in outcome.block for term in case.expected_block_terms
    ):
        return False
    if case.expected_block_absent and any(
        term in outcome.block for term in case.expected_block_absent
    ):
        return False
    if case.expected_cited:
        return (
            bool(set(case.expected_cited) & set(outcome.cited))
            and outcome.gate != "no_relevant_memory"
        )
    # a route-only or block-only case: no citation was seeded, so the earlier
    # checks are the whole contract
    return True


# ---------------------------------------------------------------------------
# latency, memory and the deadline arm
# ---------------------------------------------------------------------------


def percentile(values: list[float], fraction: float) -> float:
    """The value under `fraction` of a small sample, linear-interpolated."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latency_stats(outcomes: list[Outcome]) -> dict[str, float]:
    samples = [outcome.latency_ms for outcome in outcomes]
    return {
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
    }


def peak_index_bytes(cases: list[GoldenCase]) -> int:
    """Peak traced memory while building every case's index — the hot set's
    own footprint, not the interpreter's."""
    tracemalloc.start()
    try:
        keep: list[QueryPipeline] = [build_pipeline(case, vectors=False) for case in cases]
        _, peak = tracemalloc.get_traced_memory()
        del keep
    finally:
        tracemalloc.stop()
    return peak


def deadline_ok(case: GoldenCase) -> bool | None:
    """The deadline arm: the phrase resolves to the absolute instant, and the
    attention line reads "overdue since ..." once that instant has passed."""
    if not case.deadline_phrase:
        return None
    resolved = normalize_deadline(case.deadline_phrase, occurred_at=NOW, timezone=TIMEZONE)
    if resolved is None or case.deadline_at is None:
        return False
    if resolved.at.astimezone(UTC) != case.deadline_at.astimezone(UTC):
        return False
    if case.deadline_render_at is None or not case.deadline_render:
        return True
    views = Views(MemoryIndex(), timezone=TIMEZONE)
    line = views.render_attention(
        [
            AttentionItem(
                event_id=15,
                source="gmail",
                kind="mail",
                occurred_at=NOW,
                urgency=2,
                deadline_at=resolved.at,
                summary="send the invoice",
            )
        ],
        now=case.deadline_render_at,
    )
    return case.deadline_render in line


@dataclass
class ArmReport:
    """One arm's whole result, ready to print or gate on."""

    name: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def metrics(self) -> dict[str, dict[str, float]]:
        return metrics(self.outcomes)


def paraphrase_gain(reports: dict[str, ArmReport]) -> float:
    """The recall@5 the vector arm adds on the paraphrase subset."""
    lexical = reports[ARMS[0]].metrics.get("paraphrase", {}).get("recall@5", 1.0)
    vector = reports[ARMS[1]].metrics.get("paraphrase", {}).get("recall@5", 1.0)
    return vector - lexical
