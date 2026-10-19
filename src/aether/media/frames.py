"""Video frames through the workbench — ffmpeg in a throwaway sandbox.

The perception ladder's middle rung: when no video model takes a shared
video directly, the bytes travel to a cloud sandbox (the only place
ffmpeg lives), six evenly spaced frames come back, and the image model
reads those. The job charges the same media sandbox-jobs budget as every
other media job, and every reason it returns is plain words with no
paths and no ids — the payload note says what happened, and the agent
phrases the reply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..config import MediaConfig
from ..counters import DailyCounters, today
from ..workbench.base import (
    BACKGROUND,
    LABEL,
    LATENCY_CLASS,
    ORDINARY,
    SENSITIVITY,
    JobLimits,
    JobResult,
    JobSpec,
    WorkbenchError,
)

log = logging.getLogger("aether.media.frames")

# runs as `python frames.py` in the sandbox workdir, next to the video
# the spec carried in as input.*. ffmpeg probes content, not names, so
# the extension only has to be stable. stdout is the job's one honest
# report; the frames themselves ride back as artifacts.
FRAMES_SCRIPT = """\
import glob
import json
import os
import shutil
import subprocess

OUT = "artifacts"
FRAME_COUNT = 6
MAX_WIDTH = 768


def say(**facts):
    print(json.dumps(facts))


def main():
    os.makedirs(OUT, exist_ok=True)
    matches = sorted(glob.glob("input.*"))
    if not matches:
        say(watched=False, reason="the video never reached the sandbox")
        return
    video = matches[0]
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        say(watched=False, reason="ffmpeg is not installed in the sandbox")
        return
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width:format=duration",
            "-of", "json", video,
        ],
        capture_output=True,
        text=True,
    )
    try:
        facts = json.loads(probe.stdout)
        duration = float(facts["format"]["duration"])
        streams = facts.get("streams") or [{}]
        width = int(streams[0].get("width") or 0)
    except Exception:
        say(watched=False, reason="the video could not be read")
        return
    if duration <= 0:
        say(watched=False, reason="the video is empty")
        return
    target = min(MAX_WIDTH, width) if width else MAX_WIDTH
    made = 0
    for i in range(FRAME_COUNT):
        at = duration * (i + 0.5) / FRAME_COUNT
        run = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", video,
                "-frames:v", "1", "-vf", f"scale={target}:-2", "-q:v", "3",
                f"{OUT}/frame_{i:02d}.jpg",
            ],
            capture_output=True,
            text=True,
        )
        if run.returncode == 0:
            made += 1
    if made == 0:
        say(watched=False, reason="no frames could be pulled from the video")
        return
    say(watched=True, duration=round(duration, 1), frames=made)


main()
"""

# the binary input's name, by sniffed video type — ffmpeg probes content,
# the extension just keeps the file recognizable at a glance
_INPUT_EXTS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
}


def build_frames_spec(video: bytes, media_type: str, label: str) -> JobSpec:
    """One frame job: the video rides in as binary bytes, evenly spaced
    JPEGs ride back as artifacts. The label rides the audit meta; the
    script digest stays the script's own — binary inputs are deliberately
    outside it, so the audit record stays stable across videos."""
    ext = _INPUT_EXTS.get(media_type, "mp4")
    return JobSpec(
        script_name="frames.py",
        files={"frames.py": FRAMES_SCRIPT},
        binary_files={f"input.{ext}": video},
        limits=JobLimits(timeout_seconds=180),
        meta={LABEL: label, SENSITIVITY: ORDINARY, LATENCY_CLASS: BACKGROUND},
    )


@dataclass(frozen=True)
class WatchOutcome:
    """What one watch attempt produced — the frames as JPEG bytes when it
    worked, or the plain reason it didn't. The reason never carries a
    path, an id, or a provider's raw stderr."""

    frames: dict[str, bytes] = field(default_factory=dict)
    duration_seconds: float | None = None
    reason: str = ""


class FrameWatcher:
    """Runs one frame job per video through the workbench ladder (the
    same `_run_job` every other job travels), under the media
    sandbox-jobs budget. Over budget is a plain refusal before any job
    runs; a dispatch no provider could take is a plain reason, never a
    crash; the budget is charged only when a job actually ran."""

    def __init__(
        self,
        runner: Callable[[JobSpec], Awaitable[JobResult]],
        counters: DailyCounters,
        config: MediaConfig,
    ) -> None:
        self._runner = runner
        self._counters = counters
        self._config = config

    async def watch(self, video: bytes, media_type: str, label: str) -> WatchOutcome:
        spent = await self._counters.value(today(), "sandbox_jobs")
        if spent >= self._config.max_sandbox_jobs_per_day:
            return WatchOutcome(reason="today's video job budget is spent")
        spec = build_frames_spec(video, media_type, label)
        try:
            result = await self._runner(spec)
        except WorkbenchError as exc:
            log.info("frame job found no workbench: %s", exc)
            return WatchOutcome(reason="no cloud sandbox could take the job")
        await self._counters.bump(today(), "sandbox_jobs")
        return self._outcome(result)

    def _outcome(self, result: JobResult) -> WatchOutcome:
        """A finished job as an outcome — the script's own stdout report
        first (its reasons are already plain words), then the artifacts.
        A timed-out or failed job is a real result, never a retry."""
        if result.timed_out:
            return WatchOutcome(reason="the frame job ran past its time limit")
        try:
            report = json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            report = {}
        if not result.exit_ok:
            reason = report.get("reason")
            return WatchOutcome(
                reason=reason if isinstance(reason, str) and reason else "the frame job failed"
            )
        frames = {name: data for name, data in result.artifacts.items() if name.endswith(".jpg")}
        if not frames:
            reason = report.get("reason")
            return WatchOutcome(
                reason=reason if isinstance(reason, str) and reason else "no frames came back"
            )
        duration = report.get("duration")
        return WatchOutcome(
            frames=frames,
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        )
