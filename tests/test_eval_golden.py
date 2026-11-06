"""The golden-set runner (plan.md section 11): metrics and the CI gate.

Runs every case in eval_golden.py through the real routing, lexical index,
fusion, gate and packing via the shared helpers in eval_ablation.py. The
assertions are the gate: a regression in routes, citations, abstention or
the plain pack fails the build rather than merely printing a lower number.
The vectors-on arm and the embedding decision live in test_eval_ablation.py.
"""

from __future__ import annotations

import pytest
from eval_ablation import failure_lines, metrics, outcome_ok, run_arm
from eval_golden import CASES

from aether.memory.retrieve import RetrievalConfig


async def test_the_golden_set_holds_the_gate() -> None:
    outcomes = await run_arm(CASES, vectors=None)
    report = metrics(outcomes)
    overall = report["overall"]
    failures = failure_lines(outcomes)

    assert not failures, "golden-set failures:\n" + "\n".join(failures)
    assert overall["recall@5"] == pytest.approx(1.0)
    assert overall["mrr"] >= 0.9
    assert overall["route_accuracy"] == pytest.approx(1.0)
    assert overall["abstain_recall"] == pytest.approx(1.0)
    assert overall["abstain_precision"] == pytest.approx(1.0)
    assert overall["claim_support"] == pytest.approx(1.0)


def test_every_case_is_tagged_and_has_a_shape() -> None:
    known = {
        "attention",
        "reference",
        "recall",
        "paraphrase",
        "abstain",
        "commitment",
        "doc",
        "deadline",
    }
    assert CASES, "the golden set is empty"
    for case in CASES:
        assert case.subset in known, f"{case.name}: unknown subset {case.subset!r}"
        assert case.expected_routes or case.expect_abstain, case.name


def test_the_retrieval_config_defaults_match_the_plan() -> None:
    # the eval tunes config, not code — a silent default drift is caught here
    cfg = RetrievalConfig()
    assert cfg.rrf_k == 60
    assert cfg.mmr_lambda == 0.7
    assert outcome_ok  # the shared per-case contract is importable here
