"""Video editing through the workbench — the same ffmpeg-in-a-sandbox
seam the frame job uses, as a second consumer.

A fixed template of operations, never raw ffmpeg: every operation and
its numbers are validated here in plain words before anything travels,
the sandbox script assembles the exact invocation from those validated
params alone, and the edited copy comes back as one artifact, stored
under a new media id. Editing has no degraded fallback (understanding
falls back to a text description; an edit either happens or it doesn't),
so the editor asks up front whether any sandbox on the ladder has
ffmpeg, and refuses honestly when none does. Every job charges the same
media sandbox-jobs budget as the frame job, and every reason a refusal
carries is plain words with no paths and no ids.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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
from .frames import _INPUT_EXTS
from .store import MediaRecord, MediaRef, MediaStore, MediaStoreFull

log = logging.getLogger("aether.media.editing")

# runs as `python edit.py` in the sandbox workdir, next to the video the
# spec carried in as input.* (and a second as input2.* for concat). The
# op and its numbers arrive validated, in params.json — the script only
# ever assembles an argument vector from them, never a shell and never
# text from the caller. stdout is the job's one honest report; the edited
# file rides back as an artifact, and anything over the artifact size
# cap is reported rather than kept.
EDIT_SCRIPT = """\
import json
import os
import shutil
import subprocess

OUT = "artifacts"
OUT_LIMIT = 20 * 1024 * 1024


def say(**facts):
    print(json.dumps(facts))


def argv_for(op, p, video):
    # the exact ffmpeg call for one validated op — an argument vector
    # assembled from checked numbers only
    if op == "trim":
        return (
            ["ffmpeg", "-y", "-v", "error",
             "-ss", "%.3f" % p["start"], "-i", video,
             "-t", "%.3f" % (p["end"] - p["start"]),
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"],
            "mp4",
        )
    if op == "speed":
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "json", video],
            capture_output=True, text=True,
        )
        try:
            has_audio = bool(json.loads(probe.stdout)["streams"])
        except Exception:
            has_audio = False
        argv = ["ffmpeg", "-y", "-v", "error", "-i", video,
                "-filter:v", "setpts=%.6f*PTS" % (1.0 / p["factor"]),
                "-c:v", "libx264", "-preset", "veryfast"]
        # atempo needs an audio stream to work on — a silent video keeps
        # its speed and simply stays silent
        if has_audio:
            argv += ["-filter:a", "atempo=%.3f" % p["factor"]]
        return argv, "mp4"
    if op == "mute":
        return (
            ["ffmpeg", "-y", "-v", "error", "-i", video, "-an",
             "-c:v", "libx264", "-preset", "veryfast"],
            "mp4",
        )
    if op == "extract_audio":
        return (
            ["ffmpeg", "-y", "-v", "error", "-i", video, "-vn",
             "-c:a", "libmp3lame"],
            "mp3",
        )
    if op == "resize":
        return (
            ["ffmpeg", "-y", "-v", "error", "-i", video,
             "-vf", "scale=%d:%d" % (p["width"], p["height"]),
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"],
            "mp4",
        )
    if op == "convert":
        fmt = p["format"]
        if fmt == "webm":
            return (
                ["ffmpeg", "-y", "-v", "error", "-i", video,
                 "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0",
                 "-c:a", "libopus"],
                "webm",
            )
        if fmt == "gif":
            return (
                ["ffmpeg", "-y", "-v", "error", "-i", video,
                 "-vf", "fps=12,scale=480:-1:flags=lanczos"],
                "gif",
            )
        if fmt == "mp3":
            return (
                ["ffmpeg", "-y", "-v", "error", "-i", video, "-vn",
                 "-c:a", "libmp3lame"],
                "mp3",
            )
        return (
            ["ffmpeg", "-y", "-v", "error", "-i", video,
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"],
            "mp4",
        )
    return None, ""


def main():
    try:
        with open("params.json") as fh:
            p = json.load(fh)
    except OSError:
        say(edited=False, reason="the edit's parameters never reached the sandbox")
        return
    matches = sorted(n for n in os.listdir(".") if n.startswith("input."))
    if not matches:
        say(edited=False, reason="the video never reached the sandbox")
        return
    video = matches[0]
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        say(edited=False, reason="ffmpeg is not installed in the sandbox")
        return
    op = p.get("op")
    if op == "concat":
        seconds = sorted(n for n in os.listdir(".") if n.startswith("input2."))
        if not seconds:
            say(edited=False, reason="the second video never reached the sandbox")
            return
        # the list-file demuxer reads both paths from concat.txt — plain
        # names assembled here, never caller text
        with open("concat.txt", "w") as fh:
            for name in (video, seconds[0]):
                fh.write("file '%s'\\n" % name)
        argv = ["ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0", "-i", "concat.txt",
                "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
        ext = "mp4"
    else:
        argv, ext = argv_for(op, p, video)
        if argv is None:
            say(edited=False, reason="that edit is not one of the fixed operations")
            return
    os.makedirs(OUT, exist_ok=True)
    out = "output." + ext
    run = subprocess.run(argv + [out], capture_output=True, text=True)
    if run.returncode != 0:
        say(edited=False, reason="ffmpeg refused that edit")
        return
    size = os.path.getsize(out) if os.path.exists(out) else 0
    if size == 0:
        say(edited=False, reason="the edit produced nothing")
        return
    if size > OUT_LIMIT:
        # refused honestly, and nothing over the cap is ever left for
        # the adapter to collect
        say(edited=False, reason="the edited file is over the size limit and was not kept")
        return
    os.rename(out, os.path.join(OUT, out))
    say(edited=True)


main()
"""

# the tool-facing spellings the model is offered — the internal op names
# the script reads are the script's own
OPS = ("trim", "concat", "speed", "mute", "extract-audio", "resize", "convert")

_CONVERT_FORMATS = ("mp4", "webm", "gif", "mp3")

# the longest trim we will re-encode — twelve hours, far past any clip a
# chat surface can carry, and a ceiling so a bad range can never ask for
# an unbounded transcode
_MAX_SECONDS = 43200.0


def _number(params: dict[str, Any], key: str) -> float | None:
    """A strictly numeric parameter — a bool is not a number here, and
    neither is anything the model may have sent as text."""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate_edit(operation: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """One operation and its raw params as the canonical job params, or a
    plain refusal. Every number the sandbox will use is checked here —
    the script only ever receives values that already passed, so the
    model can never smuggle an argument past the template."""
    op = operation.strip().lower()
    if op == "trim":
        start = _number(params, "start")
        end = _number(params, "end")
        if start is None or end is None:
            return None, "trim needs start and end in seconds"
        if start < 0 or end <= start or end > _MAX_SECONDS:
            return (
                None,
                "trim needs a start at or after 0 and an end after the start, within 12 hours",
            )
        return {"op": "trim", "start": start, "end": end}, ""
    if op == "concat":
        second = params.get("second_media_id")
        if not isinstance(second, int) or isinstance(second, bool) or second < 1:
            return None, "concat needs second_media_id — the media id of the other video"
        return {"op": "concat"}, ""
    if op == "speed":
        factor = _number(params, "factor")
        if factor is None or not 0.5 <= factor <= 2.0:
            return None, "speed takes a factor from 0.5 to 2"
        return {"op": "speed", "factor": factor}, ""
    if op == "mute":
        return {"op": "mute"}, ""
    if op == "extract-audio":
        return {"op": "extract_audio"}, ""
    if op == "resize":
        width = params.get("width")
        height = params.get("height")
        for value in (width, height):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 16 <= value <= 3840
                or value % 2
            ):
                return None, "resize needs width and height — even numbers from 16 to 3840"
        return {"op": "resize", "width": width, "height": height}, ""
    if op == "convert":
        fmt = str(params.get("format", "")).strip().lower()
        if fmt not in _CONVERT_FORMATS:
            return None, "convert takes one of: mp4, webm, gif, mp3"
        return {"op": "convert", "format": fmt}, ""
    return (
        None,
        "that edit is not one of the fixed operations — "
        "trim, concat, speed, mute, extract-audio, resize, convert",
    )


def build_edit_spec(
    video: bytes,
    media_type: str,
    second: tuple[bytes, str] | None,
    canonical: dict[str, Any],
    label: str,
) -> JobSpec:
    """One edit job: the video (and its concat partner) ride in as binary
    bytes, the validated op rides in as params.json, the edited file
    rides back as one artifact. The script digest stays the script's own
    — binary inputs are deliberately outside it, so the audit record
    stays stable across videos."""
    ext = _INPUT_EXTS.get(media_type, "mp4")
    binaries = {f"input.{ext}": video}
    if second is not None:
        binaries[f"input2.{_INPUT_EXTS.get(second[1], 'mp4')}"] = second[0]
    return JobSpec(
        script_name="edit.py",
        files={"edit.py": EDIT_SCRIPT, "params.json": json.dumps(canonical)},
        binary_files=binaries,
        limits=JobLimits(timeout_seconds=300),
        meta={LABEL: label, SENSITIVITY: ORDINARY, LATENCY_CLASS: BACKGROUND},
    )


@dataclass(frozen=True)
class EditOutcome:
    """What one edit produced — the stored copy's reference when it
    worked, or the plain reason it didn't. The reason never carries a
    path, an id, or a provider's raw stderr."""

    media_ref: MediaRef | None = None
    reason: str = ""


class MediaEditor:
    """Runs one edit job per call through the workbench ladder, under
    the media sandbox-jobs budget. No ffmpeg anywhere on the ladder is an
    up-front refusal before any bytes travel; over budget is a plain
    refusal before any job runs; a dispatch no provider could take is a
    plain reason, never a crash; the budget is charged only when a job
    actually ran. The edited copy is stored under a new media id —
    sending it to anyone is a separate, approval-parked act."""

    def __init__(
        self,
        runner: Callable[[JobSpec], Awaitable[JobResult]],
        counters: DailyCounters,
        config: MediaConfig,
        media: MediaStore,
        ffmpeg_capable: Callable[[], Awaitable[bool]],
    ) -> None:
        self._runner = runner
        self._counters = counters
        self._config = config
        self._media = media
        self._ffmpeg_capable = ffmpeg_capable

    async def edit(self, media_id: int, operation: str, params: dict[str, Any]) -> EditOutcome:
        canonical, refusal = validate_edit(operation, params)
        if canonical is None:
            return EditOutcome(reason=refusal)
        if not await self._ffmpeg_capable():
            return EditOutcome(
                reason=(
                    "video editing needs ffmpeg, and no cloud sandbox "
                    "that has it is available right now"
                )
            )
        spent = await self._counters.value(today(), "sandbox_jobs")
        if spent >= self._config.max_sandbox_jobs_per_day:
            return EditOutcome(reason="today's video job budget is spent")
        record = await self._media.get(media_id)
        if record is None:
            return EditOutcome(
                reason=(
                    "that video is no longer stored — it may have been "
                    "removed to make room for newer files"
                )
            )
        if not record.media_type.startswith("video/"):
            return EditOutcome(
                reason=f"that is a {record.media_type} file, not a video — editing works on videos"
            )
        second: tuple[bytes, str] | None = None
        if canonical["op"] == "concat":
            # validate_edit already proved second_media_id is a real
            # integer ≥ 1 — the store decides whether it exists
            other = await self._media.get(int(params["second_media_id"]))
            if other is None:
                return EditOutcome(
                    reason=(
                        "the second video is no longer stored — it may have "
                        "been removed to make room for newer files"
                    )
                )
            if not other.media_type.startswith("video/"):
                return EditOutcome(
                    reason=(
                        f"the second file is a {other.media_type}, not a "
                        "video — concat joins two videos"
                    )
                )
            second = (other.data, other.media_type)
        label = f"{operation} on {record.filename or 'a stored video'}"
        spec = build_edit_spec(record.data, record.media_type, second, canonical, label)
        try:
            result = await self._runner(spec)
        except WorkbenchError as exc:
            log.info("edit job found no workbench: %s", exc)
            return EditOutcome(reason="no cloud sandbox could take the job")
        await self._counters.bump(today(), "sandbox_jobs")
        return await self._outcome(result, record)

    async def _outcome(self, result: JobResult, record: MediaRecord) -> EditOutcome:
        """A finished job as an outcome — the script's own stdout report
        first (its reasons are already plain words), then the one output
        artifact. A timed-out or failed job is a real result, never a
        retry, and the edited copy goes to storage under a new id."""
        if result.timed_out:
            return EditOutcome(reason="the edit job ran past its time limit")
        try:
            report = json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            report = {}
        if not result.exit_ok:
            reason = report.get("reason")
            return EditOutcome(
                reason=reason if isinstance(reason, str) and reason else "the edit job failed"
            )
        outputs = {
            name: data for name, data in result.artifacts.items() if name.startswith("output.")
        }
        if not outputs:
            reason = report.get("reason")
            return EditOutcome(
                reason=reason if isinstance(reason, str) and reason else "the edit produced nothing"
            )
        name, data = sorted(outputs.items())[0]
        stem = (record.filename or "video").rsplit(".", 1)[0]
        ext = name.split(".", 1)[1]
        try:
            ref = await self._media.put(data, filename=f"{stem}-edited.{ext}", origin="edit")
        except MediaStoreFull:
            return EditOutcome(reason="media storage is full — the edited file could not be kept")
        return EditOutcome(media_ref=ref)
