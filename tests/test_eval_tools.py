"""The tool-choice eval: datasets, transcripts, and the scorer's verdicts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether.evals.tools import load_cases, load_transcript, score, score_directory


def _transcripts(cases) -> dict[str, list[dict]]:
    return {
        str(case["id"]): [
            {
                "name": case["expected_tool"],
                "arguments": dict(case.get("expected_args") or {}),
            }
        ]
        for case in cases
    }


def test_the_shipped_dataset_is_well_formed() -> None:
    cases = load_cases()

    assert len(cases) == 12
    assert {case["id"] for case in cases} == {str(case["id"]) for case in cases}


def test_a_correct_run_scores_one_hundred_percent() -> None:
    cases = load_cases()

    result = score(cases, _transcripts(cases))

    assert result.tool_accuracy == 1.0
    assert result.argument_accuracy == 1.0
    assert result.wrong_tool_rate == 0.0


def test_a_different_tool_is_not_credit() -> None:
    cases = load_cases()
    transcripts = _transcripts(cases)
    transcripts["alert-for-a-sender"] = [
        {"name": "mail__send_email", "arguments": {"to": "landlord@example.com"}}
    ]

    result = score(cases, transcripts)

    failed = next(score for score in result.cases if score.case_id == "alert-for-a-sender")
    assert failed.tool_ok is False
    assert failed.args_ok is False
    assert failed.wrong_tool is True
    assert result.tool_accuracy < 1.0
    assert result.wrong_tool_rate > 0.0


def test_the_right_tool_without_its_argument_is_not_credit() -> None:
    cases = load_cases()
    transcripts = _transcripts(cases)
    transcripts["alert-for-a-sender"] = [{"name": "alerts__create", "arguments": {"scope": "mail"}}]

    result = score(cases, transcripts)

    failed = next(score for score in result.cases if score.case_id == "alert-for-a-sender")
    assert failed.tool_ok is True
    assert failed.args_ok is False
    assert result.argument_accuracy < 1.0


def test_text_arguments_compare_without_case_or_padding() -> None:
    cases = load_cases()
    transcripts = _transcripts(cases)
    transcripts["alert-for-a-sender"] = [
        {
            "name": "alerts__create",
            "arguments": {"scope": "MAIL", "sender": " Landlord@Example.com "},
        }
    ]

    result = score(cases, transcripts)

    passed = next(score for score in result.cases if score.case_id == "alert-for-a-sender")
    assert passed.args_ok is True


def test_a_recorded_directory_is_read_by_case_id(tmp_path: Path) -> None:
    (tmp_path / "alert-for-a-sender.json").write_text(
        json.dumps(
            [
                {
                    "name": "alerts__create",
                    "arguments": {"scope": "mail", "sender": "landlord@example.com"},
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "reminder-at-a-time.json").write_text(
        json.dumps({"calls": [{"name": "alerts__create", "arguments": {"kind": "reminder"}}]}),
        encoding="utf-8",
    )

    transcripts = load_transcript(tmp_path)
    assert set(transcripts) == {"alert-for-a-sender", "reminder-at-a-time"}

    cases = [case for case in load_cases() if case["id"] in transcripts]
    result = score_directory(cases, tmp_path)
    assert result.tool_accuracy == 1.0


def test_a_malformed_dataset_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "tools.yaml"
    bad.write_text("cases:\n  - id: x\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_cases(bad)
