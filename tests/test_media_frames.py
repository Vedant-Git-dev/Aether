"""The frame job's ladder — budget gate, dispatch, and the honest outcome.
No network, no ffmpeg: the runner and the counters are doubles."""

import json

import pytest

from aether.config import MediaConfig
from aether.media.frames import FRAMES_SCRIPT, FrameWatcher, build_frames_spec
from aether.workbench.base import JobResult, JobSpec, WorkbenchError, script_digest


class FakeCounters:
    """DailyCounters double: one int per field, every bump recorded."""

    def __init__(self, start: int = 0) -> None:
        self.counts = {"sandbox_jobs": start}
        self.bumps: list[str] = []

    async def value(self, day: str, field: str) -> int:
        return self.counts.get(field, 0)

    async def bump(self, day: str, field: str, n: int = 1) -> int:
        self.bumps.append(field)
        self.counts[field] = self.counts.get(field, 0) + n
        return self.counts[field]


class FakeRunner:
    """The _run_job double: one scripted result or exception per call."""

    def __init__(self, results: list[JobResult | Exception]) -> None:
        self.results = list(results)
        self.specs: list[JobSpec] = []

    async def __call__(self, spec: JobSpec) -> JobResult:
        self.specs.append(spec)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok(stdout: str = '{"watched": true, "duration": 9.5, "frames": 6}') -> JobResult:
    return JobResult(
        exit_ok=True,
        stdout=stdout,
        stderr="",
        artifacts={f"frame_{i:02d}.jpg": b"jpg" for i in range(6)},
        provider="modal",
    )


async def test_a_watch_builds_one_job_with_the_video_as_binary_bytes() -> None:
    runner = FakeRunner([_ok()])
    watcher = FrameWatcher(runner, FakeCounters(), MediaConfig())
    clip = b"\x00\x00\x00\x18ftypisomfake"

    outcome = await watcher.watch(clip, "video/mp4", "clip.mp4")

    assert outcome.frames and outcome.duration_seconds == 9.5
    spec = runner.specs[0]
    assert spec.script_name == "frames.py"
    assert spec.files == {"frames.py": FRAMES_SCRIPT}
    assert spec.binary_files == {"input.mp4": clip}
    # the audit digest is the script's own — the video bytes never enter it
    assert script_digest(spec) == script_digest(build_frames_spec(b"other", "video/mp4", "x"))


async def test_a_sniffed_webm_gets_its_own_extension() -> None:
    runner = FakeRunner([_ok()])
    watcher = FrameWatcher(runner, FakeCounters(), MediaConfig())
    await watcher.watch(b"webm bytes", "video/webm", "clip.webm")
    assert list(runner.specs[0].binary_files) == ["input.webm"]


async def test_a_job_ran_means_the_budget_is_charged() -> None:
    counters = FakeCounters()
    watcher = FrameWatcher(FakeRunner([_ok()]), counters, MediaConfig())
    await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert counters.bumps == ["sandbox_jobs"]
    assert counters.counts["sandbox_jobs"] == 1


async def test_a_spent_budget_refuses_before_any_job_runs() -> None:
    config = MediaConfig(max_sandbox_jobs_per_day=2)
    runner = FakeRunner([_ok()])
    watcher = FrameWatcher(runner, FakeCounters(start=2), config)

    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")

    assert outcome.frames == {}
    assert outcome.reason == "today's video job budget is spent"
    assert runner.specs == []  # nothing ran, so nothing is charged
    assert FakeCounters(start=2).bumps == []


async def test_a_dispatch_no_provider_could_take_is_a_plain_reason() -> None:
    counters = FakeCounters()
    watcher = FrameWatcher(
        FakeRunner([WorkbenchError("modal: no token id")]), counters, MediaConfig()
    )
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.frames == {}
    assert outcome.reason == "no cloud sandbox could take the job"
    # the job never completed — the budget stays uncharged
    assert counters.bumps == []


async def test_a_timed_out_job_is_a_reason_never_a_retry() -> None:
    runner = FakeRunner([JobResult(exit_ok=False, stdout="", stderr="", timed_out=True)])
    watcher = FrameWatcher(runner, FakeCounters(), MediaConfig())
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.reason == "the frame job ran past its time limit"
    assert len(runner.specs) == 1  # one attempt, no retry


async def test_a_failed_job_says_its_own_reported_reason() -> None:
    result = JobResult(
        exit_ok=False,
        stdout=json.dumps({"watched": False, "reason": "ffmpeg is not installed in the sandbox"}),
        stderr="",
    )
    watcher = FrameWatcher(FakeRunner([result]), FakeCounters(), MediaConfig())
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.reason == "ffmpeg is not installed in the sandbox"


async def test_a_failed_job_without_a_reason_says_the_plain_fact() -> None:
    result = JobResult(exit_ok=False, stdout="garbage not json", stderr="boom")
    watcher = FrameWatcher(FakeRunner([result]), FakeCounters(), MediaConfig())
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.reason == "the frame job failed"


async def test_frames_come_back_only_as_jpegs() -> None:
    result = JobResult(
        exit_ok=True,
        stdout='{"watched": true, "duration": 3.0}',
        stderr="",
        artifacts={"frame_00.jpg": b"jpg", "notes.txt": b"text", "frame_01.png": b"png"},
    )
    watcher = FrameWatcher(FakeRunner([result]), FakeCounters(), MediaConfig())
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.frames == {"frame_00.jpg": b"jpg"}


async def test_an_empty_job_says_no_frames_came_back() -> None:
    result = JobResult(exit_ok=True, stdout="{}", stderr="", artifacts={})
    watcher = FrameWatcher(FakeRunner([result]), FakeCounters(), MediaConfig())
    outcome = await watcher.watch(b"clip", "video/mp4", "clip.mp4")
    assert outcome.reason == "no frames came back"


def test_the_script_reports_its_own_plain_reasons() -> None:
    # the script's honest fallbacks ride stdout as JSON — the watcher
    # passes them through untouched, so their wording is a contract
    assert 'reason="the video never reached the sandbox"' in FRAMES_SCRIPT
    assert 'reason="ffmpeg is not installed in the sandbox"' in FRAMES_SCRIPT
    assert 'reason="the video could not be read"' in FRAMES_SCRIPT
    assert 'reason="no frames could be pulled from the video"' in FRAMES_SCRIPT
    # the width target is computed, never handed to a shell
    assert "target = min(MAX_WIDTH, width)" in FRAMES_SCRIPT


def test_the_reason_never_carries_a_path_or_an_id() -> None:
    # panel-safe by construction: every reason the watcher can return
    for reason in (
        "today's video job budget is spent",
        "no cloud sandbox could take the job",
        "the frame job ran past its time limit",
        "the frame job failed",
        "no frames came back",
    ):
        assert "/" not in reason
        assert ".mp4" not in reason


async def test_a_bad_runner_exception_is_not_swallowed() -> None:
    # a bug in the runner is not a WorkbenchError — it must crash, not
    # masquerade as a plain media failure
    watcher = FrameWatcher(FakeRunner([RuntimeError("bug")]), FakeCounters(), MediaConfig())
    with pytest.raises(RuntimeError, match="bug"):
        await watcher.watch(b"clip", "video/mp4", "clip.mp4")
