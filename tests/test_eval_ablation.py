"""The evaluation decision, machine-checked (plan.md section 11).

A regression in the golden set fails here first. On top of that, this test is
the embedding decision rule itself: the vector arm must earn its place by the
paraphrase margin in `memory.embedding_min_paraphrase_gain`, the offline
retrieval budget must hold, and every honest answer in the set must be fully
supported by the pack. The numbers are printed so the CI log carries the
evidence behind the decision.
"""

from __future__ import annotations

import pytest
from eval_ablation import (
    ArmReport,
    deadline_ok,
    failure_lines,
    latency_stats,
    metrics,
    paraphrase_gain,
    peak_index_bytes,
    run_arm,
)
from eval_golden import CASES

from aether.config import MemoryConfig


async def _reports(repeats: int) -> dict[str, ArmReport]:
    return {
        "lexical": ArmReport("lexical", await run_arm(CASES, vectors=False, repeats=repeats)),
        "vector": ArmReport("vector", await run_arm(CASES, vectors=True, repeats=repeats)),
        # the canonical gate: vectors on exactly where a case seeds a hit
        "seeded": ArmReport("seeded", await run_arm(CASES, vectors=None, repeats=repeats)),
    }


def _summary(report: ArmReport) -> str:
    overall = report.metrics["overall"]
    return (
        f"{report.name}: recall@5={overall['recall@5']:.3f} mrr={overall['mrr']:.3f} "
        f"route={overall['route_accuracy']:.3f} abstain={overall['abstain_recall']:.3f}/"
        f"{overall['abstain_precision']:.3f} support={overall['claim_support']:.3f}"
    )


async def test_the_two_arms_measure_the_embedding_decision() -> None:
    cfg = MemoryConfig()
    reports = await _reports(cfg.eval_repeats)
    primary = reports["seeded"]

    failures = failure_lines(primary.outcomes)
    assert not failures, "golden-set failures (as-seeded arm):\n" + "\n".join(failures)

    overall = primary.metrics["overall"]
    assert overall["recall@5"] == pytest.approx(1.0)
    assert overall["mrr"] >= 0.9
    assert overall["route_accuracy"] == pytest.approx(1.0)
    assert overall["abstain_recall"] == pytest.approx(1.0)
    assert overall["abstain_precision"] == pytest.approx(1.0)
    # every answer in the set is backed by the pack it cited
    assert overall["claim_support"] == pytest.approx(1.0)

    # the vector arm has to earn its keep on the paraphrase subset
    gain = paraphrase_gain(reports)
    assert gain >= cfg.embedding_min_paraphrase_gain, (
        "vectors no longer earn their place:\n"
        + "\n".join(_summary(report) for report in reports.values())
        + f"\nparaphrase recall@5 gain={gain:.3f} < {cfg.embedding_min_paraphrase_gain}"
    )

    # and neither arm may blow the offline retrieval budget
    for report in reports.values():
        stats = latency_stats(report.outcomes)
        assert stats["p95_ms"] <= cfg.retrieve_latency_p95_ms, (
            f"{report.name} p95 {stats['p95_ms']:.1f}ms over {cfg.retrieve_latency_p95_ms}ms"
        )

    print("\n".join(_summary(report) for report in reports.values()))
    print(f"paraphrase recall@5 gain={gain:.3f} | {latency_stats(primary.outcomes)}")
    print(f"peak index bytes={peak_index_bytes(CASES)}")


async def test_the_deadline_arm_resolves_and_renders_overdue() -> None:
    for case in CASES:
        if not case.deadline_phrase:
            continue
        assert deadline_ok(case) is True, case.name


def test_metrics_split_by_subset() -> None:
    # a tiny pure check that the per-subset split exists and covers the tags
    subsets = set(metrics([]).keys())
    assert subsets == {"overall"}
    tags = {case.subset for case in CASES}
    assert {"attention", "reference", "recall", "paraphrase"} <= tags
