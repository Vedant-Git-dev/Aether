"""The golden-set runner (plan.md section 11): metrics and the CI gate.

Runs every case in eval_golden.py through the real routing, lexical index,
fusion, gate and packing, with the paraphrase case given its fake vector
hit. The assertions are the gate: a regression in routes, citations or
abstention fails the build rather than merely printing a lower number.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from eval_golden import CASES, NOW, GoldenCase

from aether.memory.indexes import MemoryIndex
from aether.memory.lexical import LexicalIndex
from aether.memory.pipeline import QueryPipeline
from aether.memory.retrieve import RetrievalConfig
from aether.memory.views import Views


@dataclass
class Outcome:
    case: GoldenCase
    routes: tuple[str, ...]
    gate: str
    cited: list[int]
    resolved_kind: str


def _build(case: GoldenCase) -> QueryPipeline:
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
    vectors = None
    if case.vector_hits:
        from fakes import FakeVectorMemory

        vectors = FakeVectorMemory(hits=list(case.vector_hits))
    return QueryPipeline(
        lexical=lexical,
        index=index,
        views=Views(index, timezone="Asia/Kolkata"),
        vectors=vectors,
        cfg=RetrievalConfig(),
        timezone="Asia/Kolkata",
    )


async def _run(case: GoldenCase) -> Outcome:
    pipeline = _build(case)
    result = await pipeline.run(
        case.question,
        now=NOW,
        focus=case.focus,
        entity_names=case.entity_names,
    )
    cited = [int(entry["id"]) for entry in result.packed.trace if "id" in entry]
    resolved_kind = result.routing.resolved.kind if result.routing.resolved else ""
    return Outcome(
        case=case,
        routes=result.routing.routes,
        gate=result.packed.gate,
        cited=cited,
        resolved_kind=resolved_kind,
    )


def _metrics(outcomes: list[Outcome]) -> dict[str, float]:
    routed = [
        set(outcome.case.expected_routes) <= set(outcome.routes)
        for outcome in outcomes
        if outcome.case.expected_routes
    ]
    recall: list[float] = []
    reciprocal: list[float] = []
    abstain_true = [outcome for outcome in outcomes if outcome.case.expect_abstain]
    # only answerable retrieval cases count toward "did not wrongly abstain";
    # route-only cases (a commitments question with no store here) have no
    # evidence to abstain from and are judged by their route alone
    answerable = [outcome for outcome in outcomes if outcome.case.expected_cited]
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
    abstain_ok = sum(1 for outcome in abstain_true if outcome.gate == "no_relevant_memory")
    non_abstain_ok = sum(1 for outcome in answerable if outcome.gate != "no_relevant_memory")
    return {
        "recall@5": sum(recall) / len(recall) if recall else 1.0,
        "mrr": sum(reciprocal) / len(reciprocal) if reciprocal else 1.0,
        "route_accuracy": sum(routed) / len(routed) if routed else 1.0,
        "abstain_recall": abstain_ok / len(abstain_true) if abstain_true else 1.0,
        # precision here is "did not wrongly abstain on answerable cases"
        "abstain_precision": non_abstain_ok / len(answerable) if answerable else 1.0,
    }


async def test_the_golden_set_holds_the_gate() -> None:
    outcomes = [await _run(case) for case in CASES]
    metrics = _metrics(outcomes)
    failures = [
        f"{outcome.case.name}: routes={outcome.routes} gate={outcome.gate} "
        f"cited={outcome.cited} resolved={outcome.resolved_kind}"
        for outcome in outcomes
        if not _outcome_ok(outcome)
    ]

    assert not failures, "golden-set failures:\n" + "\n".join(failures)
    assert metrics["recall@5"] == pytest.approx(1.0)
    assert metrics["mrr"] >= 0.9
    assert metrics["route_accuracy"] == pytest.approx(1.0)
    assert metrics["abstain_recall"] == pytest.approx(1.0)
    assert metrics["abstain_precision"] == pytest.approx(1.0)


def _outcome_ok(outcome: Outcome) -> bool:
    case = outcome.case
    if case.expected_routes and not set(case.expected_routes) <= set(outcome.routes):
        return False
    if case.expected_resolved:
        return outcome.gate == "resolved" and outcome.resolved_kind == case.expected_resolved
    if case.expect_abstain:
        return outcome.gate == "no_relevant_memory"
    if case.expected_cited:
        return (
            bool(set(case.expected_cited) & set(outcome.cited))
            and outcome.gate != "no_relevant_memory"
        )
    # a route-only case: no evidence was seeded, so only the route counts
    return True
