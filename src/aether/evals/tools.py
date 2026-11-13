"""Tool-choice and argument accuracy, scored from recorded transcripts.

The report that started this work found no measurement of which tool the
model picks or whether its arguments are right. This is that measurement,
kept structural: a case names the expected call and an argument subset, a
transcript supplies what a run actually proposed, and the scorer reports
tool accuracy, argument accuracy and a wrong-tool rate.

It never calls a model. Record a run (one JSON file per case under a
directory, or a mapping in memory), score it, and the numbers are
repeatable across providers and prompts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DATASET = Path(__file__).with_name("tools.yaml")


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    tool_ok: bool
    args_ok: bool
    wrong_tool: bool


@dataclass(frozen=True)
class ToolEvalResult:
    total: int
    tool_correct: int
    args_correct: int
    wrong_tool: int
    cases: tuple[CaseScore, ...]

    @property
    def tool_accuracy(self) -> float:
        return self.tool_correct / self.total if self.total else 0.0

    @property
    def argument_accuracy(self) -> float:
        return self.args_correct / self.total if self.total else 0.0

    @property
    def wrong_tool_rate(self) -> float:
        return self.wrong_tool / self.total if self.total else 0.0


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path is not None else _DATASET
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("the tool dataset must contain cases")
    for case in cases:
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError("every tool case needs an id")
        if not case.get("expected_tool"):
            raise ValueError(f"case {case['id']!r} has no expected_tool")
        args = case.get("expected_args")
        if args is not None and not isinstance(args, dict):
            raise ValueError(f"case {case['id']!r} expected_args must be a mapping")
        forbidden = case.get("forbidden_tools")
        if forbidden is not None and not isinstance(forbidden, list):
            raise ValueError(f"case {case['id']!r} forbidden_tools must be a list")
    return cases


def load_transcript(directory: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Read recorded calls, one `<case-id>.json` per case.

    The file is either a list of calls or an object with a `calls` list;
    each call is `{"name": ..., "arguments": {...}}`.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(Path(directory).glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        calls = raw.get("calls") if isinstance(raw, dict) else raw
        if not isinstance(calls, list):
            raise ValueError(f"{path.name}: expected a list of calls")
        found[path.stem] = calls
    return found


def _normalise(name: str) -> str:
    return (name or "").strip()


def _args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        if isinstance(value, str) and isinstance(actual[key], str):
            if value.strip().casefold() != actual[key].strip().casefold():
                return False
        elif actual[key] != value:
            return False
    return True


def score(
    cases: list[dict[str, Any]],
    transcripts: dict[str, list[dict[str, Any]]],
) -> ToolEvalResult:
    """Score one run's calls against the labelled cases."""
    scores: list[CaseScore] = []
    for case in cases:
        calls = transcripts.get(str(case["id"])) or []
        expected_tool = _normalise(str(case["expected_tool"]))
        expected_args = dict(case.get("expected_args") or {})
        forbidden = {_normalise(str(name)) for name in case.get("forbidden_tools") or []}

        first = calls[0] if calls else {}
        name = _normalise(str(first.get("name") or ""))
        arguments = first.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}

        tool_ok = name == expected_tool
        args_ok = tool_ok and _args_match(expected_args, arguments)
        wrong = any(_normalise(str(call.get("name") or "")) in forbidden for call in calls)
        scores.append(
            CaseScore(
                case_id=str(case["id"]),
                tool_ok=tool_ok,
                args_ok=args_ok,
                wrong_tool=wrong,
            )
        )
    return ToolEvalResult(
        total=len(scores),
        tool_correct=sum(1 for score in scores if score.tool_ok),
        args_correct=sum(1 for score in scores if score.args_ok),
        wrong_tool=sum(1 for score in scores if score.wrong_tool),
        cases=tuple(scores),
    )


def score_directory(cases: list[dict[str, Any]], directory: str | Path) -> ToolEvalResult:
    return score(cases, load_transcript(directory))
