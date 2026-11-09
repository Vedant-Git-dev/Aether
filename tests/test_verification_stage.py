"""Verification is data; the composer owns the words.

Two leaks this file pins:

- the verifier used to append its own caveat and replace bad quotes with a
  marker, so internal verification language reached the person;
- an internal finding id or source tag could survive into the delivered
  answer.

The contract: `find_findings` returns records only, the fixed sentences
come from the message catalog, and a value-based guard blocks any internal
identity that reaches the outbound text — regenerating once with the
user-safe view, then refusing delivery.
"""

from __future__ import annotations

from fakes import FakeProvider
from test_agent_loop import LoopKit, _msg
from test_query_pipeline_loop import INVOICE, _config, _pipeline

from aether.agent.composer import compose_answer, deterministic_compose, tool_call_for_testing
from aether.agent.leaks import LeakGuard, collect_internal_values
from aether.memory import verify as verify_module
from aether.memory.verify import Finding, find_findings, unsupported
from aether.message_catalog import catalog

CHUNKS = {1: ["[E1 | mail] The invoice is due on Friday."]}


def _answer(text: str):
    from aether.llm.types import ToolCall, Turn

    return Turn(tool_calls=[ToolCall(id="r1", name="reply_user", arguments={"reply": text})])


# ---------------------------------------------------------------------------
# 6. the verifier produces records, never user-facing words
# ---------------------------------------------------------------------------


def test_the_verifier_returns_records_and_no_user_text() -> None:
    findings = find_findings(
        'He said "the payment cleared yesterday" [E9].',
        chunks_by_event=CHUNKS,
        known_event_ids={1},
    )

    assert all(isinstance(finding, Finding) for finding in findings)
    assert {finding.kind for finding in unsupported(findings)} == {"quote", "id"}
    # the structured record carries the claim and its sources, nothing that
    # reads as a sentence to the person
    for finding in findings:
        assert set(finding.as_dict()) == {
            "finding_id",
            "kind",
            "claim",
            "supported",
            "source_ids",
        }


def test_the_verifier_has_no_repair_or_feedback_path_left() -> None:
    # the old leak was structural: a repair helper that wrote user text and a
    # feedback string appended to a regenerated answer. Neither may exist.
    assert not hasattr(verify_module, "repair_reply")
    assert not hasattr(verify_module, "feedback")
    assert not hasattr(verify_module, "UNSOURCED_HEDGE")
    assert not hasattr(verify_module, "QUOTE_MARKER")


def test_fixed_caveats_come_from_the_catalog() -> None:
    reply = 'They said "the payment cleared yesterday".'
    findings = unsupported(find_findings(reply, chunks_by_event=CHUNKS, known_event_ids={1}))

    composed = deterministic_compose(reply, findings)

    assert "the payment cleared yesterday" not in composed
    assert composed.endswith(catalog().render("verification.caveat", count=1))


def test_the_catalog_falls_back_to_the_default_locale() -> None:
    assert catalog("xx").render("verification.safe_fallback") == catalog("en").render(
        "verification.safe_fallback"
    )


# ---------------------------------------------------------------------------
# 7. planted internal values are blocked and regenerated once
# ---------------------------------------------------------------------------


async def test_a_planted_finding_id_is_blocked_and_regenerated_once() -> None:
    provider = FakeProvider(
        [
            _answer('He said "the payment cleared yesterday" [E1].'),
            # the composer echoes an internal finding id
            tool_call_for_testing(
                "Rahul's invoice is due on Friday, see finding-2.",
                resolutions={"finding-2": "remove"},
            ),
            # the safe view produces clean words
            tool_call_for_testing("Rahul's invoice is due on Friday."),
        ]
    )
    kit = LoopKit(provider, config=_config(), query_pipeline=_pipeline((1, 1, INVOICE)))

    kit.loop.submit_message(_msg("what did Rahul say about the invoice?"))
    await kit.loop._tick()

    assert kit.connector.sent == ["Rahul's invoice is due on Friday."]
    assert len(provider.calls) == 3  # answer, composer, one safe regeneration
    record = kit.traces.created[-1]["payload"]["verification"]
    assert record["leak"]["blocked"] == ["finding-2"]
    assert record["leak"]["regenerated"] is True
    assert record["ok"] is True


async def test_a_leak_that_survives_regeneration_never_ships() -> None:
    provider = FakeProvider(
        [
            _answer('He said "the payment cleared yesterday" [E1].'),
            tool_call_for_testing(
                "Rahul's invoice is due on Friday, see finding-2.",
                resolutions={"finding-2": "remove"},
            ),
            tool_call_for_testing("Still quoting finding-2 in the safe view."),
        ]
    )
    kit = LoopKit(provider, config=_config(), query_pipeline=_pipeline((1, 1, INVOICE)))

    kit.loop.submit_message(_msg("what did Rahul say about the invoice?"))
    await kit.loop._tick()

    assert kit.connector.sent == [catalog().render("verification.safe_fallback")]
    record = kit.traces.created[-1]["payload"]["verification"]
    assert record["leak"]["still_leaking"] == ["finding-2"]
    assert record["ok"] is False


def test_the_guard_compares_values_not_phrases() -> None:
    values = collect_internal_values(
        findings=[
            Finding(
                finding_id="finding-4",
                kind="quote",
                claim='"made up"',
                supported=False,
                source_ids=(7,),
            )
        ],
        event_ids=[7],
        extra_texts=["[internal verification findings]"],
    )
    guard = LeakGuard(values)

    assert guard.scan("see finding-4 for details")[0].value == "finding-4"
    assert guard.scan("the source was [E7]")[0].value == "[E7]"
    assert guard.scan("a normal answer about Friday") == []


async def test_the_composer_is_offered_only_structured_output() -> None:
    provider = FakeProvider([tool_call_for_testing("clean answer")])

    composed = await compose_answer(
        provider,
        "system",
        [],
        findings=[],
        candidate="candidate",
    )

    assert composed is not None and composed.reply == "clean answer"
    specs = provider.calls[0][2]
    assert [spec.name for spec in specs] == ["compose_answer"]
