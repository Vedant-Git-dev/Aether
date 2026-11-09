"""The structural leak gate and the human review sample.

Both are configuration: `evaluation.leak_rate_limit`,
`evaluation.review_sample_size` and `evaluation.review_cadence_days`. The
shipped dataset is labelled by structure only — a case says what outcome
the turn should have produced and what actually left.
"""

from __future__ import annotations

from aether.config import EvaluationConfig
from aether.evals.harness import gate, load_cases, run

LEAKY_CASE = {
    "id": "planted-decision-narration",
    "mode": "proactive",
    "expected": "none",
    "delivered": "no reply needed - well below your bar for a ping",
}


def test_the_shipped_dataset_clears_the_default_gate() -> None:
    cases = load_cases()
    config = EvaluationConfig()
    result = run(cases, config)

    assert result.total == len(cases)
    assert result.leaks == 0
    assert result.leak_rate == 0.0
    assert result.misses == 0
    assert gate(result, config) is True


def test_the_review_sample_size_is_configuration() -> None:
    cases = load_cases()
    config = EvaluationConfig(review_sample_size=3)

    result = run(cases, config)

    assert len(result.review_sample) == 3
    assert len(set(result.review_sample)) == 3
    # a bigger sample is still bounded by the dataset
    assert len(run(cases, EvaluationConfig(review_sample_size=999)).review_sample) == len(cases)


def test_a_leaky_dataset_fails_the_gate_and_a_tighter_limit_changes_it() -> None:
    cases = [*load_cases(), LEAKY_CASE]
    config = EvaluationConfig()

    result = run(cases, config)

    assert result.leaks == 1
    assert result.leak_rate > 0
    assert gate(result, config) is False
    assert gate(result, EvaluationConfig(leak_rate_limit=1.0)) is True


def test_an_internal_value_in_delivered_text_is_a_leak() -> None:
    cases = [
        {
            "id": "answer-leaks-a-finding",
            "mode": "answer",
            "expected": "reply",
            "delivered": "Here it is, finding-2.",
            "internal_values": ["finding-2"],
        }
    ]

    result = run(cases, EvaluationConfig())

    assert result.leaks == 1
    assert result.cases[0].blocked == ("finding-2",)
