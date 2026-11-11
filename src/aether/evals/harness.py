"""Structural leak measurement over labelled turns.

This is a measurement harness, not a product surface. It reads the shipped
dataset of finished turns, compares the delivered words with the outcome
the turn was supposed to produce, and reports two rates:

- ``leak_rate`` — turns where words left when the outcome was ``none``, or
  where a listed internal value appears in the delivered text;
- ``miss_rate`` — turns that produced no words when the outcome was a
  ``reply``, ``notify`` or ``ask``.

The gate and the human review sample are both configuration
(`evaluation.leak_rate_limit`, `evaluation.review_sample_size`,
`evaluation.review_cadence_days`), so tightening either is a settings
change. The dataset carries no prose judgment: every case is labelled by
its structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import EvaluationConfig

_DATASET = Path(__file__).with_name("leaks.yaml")
_EXPECTED = frozenset({"none", "reply", "notify", "ask"})


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    leaked: bool
    missed: bool
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    total: int
    leaks: int
    misses: int
    leak_rate: float
    miss_rate: float
    review_sample: tuple[str, ...]
    cases: tuple[CaseResult, ...]


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path is not None else _DATASET
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise ValueError("the leak dataset must contain a list of cases")
    for case in cases:
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError("every case needs an id")
        if case.get("expected") not in _EXPECTED:
            raise ValueError(f"case {case['id']!r} has an unknown expected outcome")
    return cases


def load_triage_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    """The labelled candidate notes for the free-text triage decision."""
    target = Path(path) if path is not None else _DATASET
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases = data.get("triage_cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise ValueError("the leak dataset must contain a list of triage cases")
    for case in cases:
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError("every triage case needs an id")
        if case.get("expected") not in ("send", "internal"):
            raise ValueError(f"triage case {case['id']!r} has an unknown decision")
    return cases


@dataclass(frozen=True)
class TriageResult:
    total: int
    correct: int
    accuracy: float


def score_triage(cases: list[dict[str, Any]], decisions: dict[str, bool]) -> TriageResult:
    """Score supplied triage decisions against the labels.

    `decisions` maps a case id to True for send and False for internal. The
    harness never invents a decision; a real run supplies the model's.
    """
    total = len(cases)
    correct = 0
    for case in cases:
        wanted = case["expected"] == "send"
        if decisions.get(str(case["id"])) is wanted:
            correct += 1
    return TriageResult(total=total, correct=correct, accuracy=(correct / total) if total else 0.0)


def _review_sample(ids: list[str], size: int) -> tuple[str, ...]:
    if size <= 0 or not ids:
        return ()
    size = min(size, len(ids))
    step = len(ids) / size
    picked = [ids[int(index * step)] for index in range(size)]
    return tuple(dict.fromkeys(picked))


def run(cases: list[dict[str, Any]], config: EvaluationConfig) -> EvalResult:
    """Measure the dataset and build the review sample from configuration."""
    results: list[CaseResult] = []
    for case in cases:
        delivered = str(case.get("delivered") or "")
        expected = str(case.get("expected") or "none")
        internal = tuple(str(value) for value in case.get("internal_values") or ())
        blocked = tuple(value for value in internal if value and value in delivered)
        leaked = bool(delivered) if expected == "none" else bool(blocked)
        missed = not delivered and expected != "none"
        results.append(
            CaseResult(
                case_id=str(case["id"]),
                leaked=leaked,
                missed=missed,
                blocked=blocked,
            )
        )
    total = len(results)
    leaks = sum(1 for result in results if result.leaked)
    misses = sum(1 for result in results if result.missed)
    return EvalResult(
        total=total,
        leaks=leaks,
        misses=misses,
        leak_rate=(leaks / total) if total else 0.0,
        miss_rate=(misses / total) if total else 0.0,
        review_sample=_review_sample(
            [result.case_id for result in results], config.review_sample_size
        ),
        cases=tuple(results),
    )


def gate(result: EvalResult, config: EvaluationConfig) -> bool:
    """Whether the measured leak rate clears the configured limit."""
    return result.leak_rate <= config.leak_rate_limit
