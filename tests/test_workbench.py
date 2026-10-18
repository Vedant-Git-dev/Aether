"""The workbench contract and routing ladder — exercised against fakes,
with no sandbox and no network anywhere near it.

What's pinned here: the fail-safe defaults (a spec that says nothing
about itself reads as sensitive and background, so only a provider that
takes sensitive work may run it), the cleanup guarantee (a job that
blows up still counts its cleanup), and the ladder — deterministic code
that asks each provider for itself, falls through on dispatch failures,
and treats a completed-but-failed script as a real result.
"""

from __future__ import annotations

import pytest

from aether.workbench import (
    BACKGROUND,
    INTERACTIVE,
    ORDINARY,
    SENSITIVE,
    FakeWorkbench,
    JobResult,
    JobSpec,
    WorkbenchError,
    ordered,
    route,
    run,
    script_digest,
)


def spec(**meta) -> JobSpec:
    return JobSpec(script_name="job.py", files={"job.py": "print('hi')"}, meta=meta)


# -- the spec's fail-safe defaults ---------------------------------------------


def test_an_unlabeled_spec_reads_as_sensitive_and_background() -> None:
    # the core fail-safe: a spec that says nothing about itself may only
    # run where a sensitive one can
    bare = spec()
    assert bare.sensitivity == SENSITIVE
    assert bare.latency_class == BACKGROUND


def test_unknown_labels_fail_safe_too() -> None:
    assert spec(sensitivity="whatever").sensitivity == SENSITIVE
    assert spec(latency_class="urgent").latency_class == BACKGROUND


def test_known_labels_pass_through() -> None:
    marked = spec(sensitivity=ORDINARY, latency_class=INTERACTIVE)
    assert marked.sensitivity == ORDINARY
    assert marked.latency_class == INTERACTIVE


def test_the_label_falls_back_to_the_script_name() -> None:
    assert spec().label == "job.py"
    assert spec(label="Caption the talk recording").label == "Caption the talk recording"


def test_the_script_digest_is_content_addressed() -> None:
    # the audit record carries this, never the text — same script, same
    # digest; one word different, new digest
    assert script_digest(spec()) == script_digest(spec())
    assert script_digest(spec()) != script_digest(
        JobSpec(script_name="job.py", files={"job.py": "print('bye')"})
    )


# -- the fake adapter -----------------------------------------------------------


async def test_the_fake_records_its_jobs_and_returns_a_default_result() -> None:
    fake = FakeWorkbench()
    result = await fake.run_job(spec())
    assert fake.jobs == [spec()]
    assert result.exit_ok
    assert result.provider == "fake"
    assert "job.py" in result.stdout


async def test_a_blown_up_job_still_cleans_up() -> None:
    # the guarantee every real adapter must keep — cleanup in finally,
    # whatever happened inside the job
    async def blow_up(_: JobSpec) -> JobResult:
        raise WorkbenchError("the sandbox never came up")

    fake = FakeWorkbench(handler=blow_up)
    with pytest.raises(WorkbenchError):
        await fake.run_job(spec())
    assert fake.cleanups == [spec()]


async def test_the_fake_can_refuse_a_class_of_work() -> None:
    # the batch-provider stand-in: it refuses sensitive work itself, so
    # the ladder never needs its name
    batch_only = FakeWorkbench(name="batch", accept=lambda s: s.sensitivity == ORDINARY)
    assert await batch_only.accepts(spec(sensitivity=ORDINARY))
    assert not await batch_only.accepts(spec(sensitivity=SENSITIVE))
    assert not await batch_only.accepts(spec())  # unlabeled fails safe


# -- the ladder ----------------------------------------------------------------


async def test_routing_picks_the_first_provider_that_claims_the_job() -> None:
    down = FakeWorkbench(name="down", available=False)
    picky = FakeWorkbench(name="picky", accept=lambda _: False)
    taking = FakeWorkbench(name="taking")
    assert await route(spec(), [down, picky, taking]) is taking


async def test_no_provider_means_nobody_takes_the_job() -> None:
    assert await route(spec(), []) is None
    assert await route(spec(), [FakeWorkbench(available=False)]) is None
    assert await route(spec(), [FakeWorkbench(accept=lambda _: False)]) is None


def test_the_configured_default_comes_first_and_the_rest_keep_order() -> None:
    e2b = FakeWorkbench(name="e2b")
    actions = FakeWorkbench(name="github_actions")
    vercel = FakeWorkbench(name="vercel")
    assert ordered([e2b, actions, vercel], "vercel") == [vercel, e2b, actions]
    assert ordered([e2b, actions, vercel], "github_actions") == [actions, e2b, vercel]
    # an unknown default changes nothing — the list order stands
    assert ordered([e2b, actions, vercel], "nope") == [e2b, actions, vercel]


async def test_a_dispatch_failure_falls_through_to_the_next_provider() -> None:
    async def quota_refused(_: JobSpec) -> JobResult:
        raise WorkbenchError("quota refused at the first provider")

    first = FakeWorkbench(name="first", handler=quota_refused)
    second = FakeWorkbench(name="second")
    result = await run(spec(), [first, second])
    assert result.provider == "second"
    assert first.cleanups == [spec()]  # the failed dispatch still cleaned up


async def test_a_completed_but_failed_script_is_a_result_not_a_fallback() -> None:
    # the job already happened — rerunning it elsewhere would be running
    # it twice, so exit_ok=False is returned as-is
    failed = FakeWorkbench(name="failed", result=JobResult(exit_ok=False, stdout="", stderr="boom"))
    never = FakeWorkbench(name="never")
    result = await run(spec(), [failed, never])
    assert result.exit_ok is False
    assert result.provider == "failed"
    assert never.jobs == []


async def test_every_provider_failing_surfaces_the_last_error() -> None:
    def refused_at(provider: str):
        async def refused(_: JobSpec) -> JobResult:
            raise WorkbenchError(f"refused at {provider}")

        return refused

    first = FakeWorkbench(name="first", handler=refused_at("first"))
    second = FakeWorkbench(name="second", handler=refused_at("second"))
    with pytest.raises(WorkbenchError, match="refused at second"):
        await run(spec(), [first, second])


async def test_nobody_taking_the_job_is_a_plain_workbench_error() -> None:
    with pytest.raises(WorkbenchError, match="no workbench provider"):
        await run(spec(), [FakeWorkbench(accept=lambda _: False)])


async def test_a_bug_is_not_a_dispatch_failure_and_propagates() -> None:
    # anything that isn't WorkbenchError is a programming error — it must
    # not be papered over with a fallback or a queue
    async def bug(_: JobSpec) -> JobResult:
        raise TypeError("a bug, not a quota")

    buggy = FakeWorkbench(name="buggy", handler=bug)
    with pytest.raises(TypeError):
        await run(spec(), [buggy, FakeWorkbench(name="second")])
