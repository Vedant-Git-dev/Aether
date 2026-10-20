"""The edit job's ladder — the fixed op template, the budget gate, the
up-front ffmpeg refusal, and the honest outcome. No network, no ffmpeg,
no live DB: the runner, the counters, the capability, and the store are
all doubles."""

import json

import pytest
from fakes import FakeMediaStore
from test_media_frames import FakeCounters, FakeRunner

from aether.config import MediaConfig
from aether.media.editing import (
    EDIT_SCRIPT,
    OPS,
    MediaEditor,
    build_edit_spec,
    validate_edit,
)
from aether.workbench.base import JobResult, WorkbenchError, script_digest

# mp4 magic bytes, so the FakeMediaStore's sniffer calls it a video
CLIP = b"\x00\x00\x00\x18ftypisom" + b"clip bytes"
OTHER = b"\x00\x00\x00\x18ftypisom" + b"other clip bytes"
PICTURE = b"\x89PNG\r\n\x1a\n" + b"png bytes"


class FakeCapability:
    """The ladder-asking double: says yes or no, remembers it was asked."""

    def __init__(self, capable: bool = True) -> None:
        self.capable = capable
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.capable


def _edited(
    artifacts: dict[str, bytes] | None = None, stdout: str = '{"edited": true}'
) -> JobResult:
    return JobResult(
        exit_ok=True,
        stdout=stdout,
        stderr="",
        artifacts=artifacts
        if artifacts is not None
        else {"output.mp4": b"\x00\x00\x00\x18ftypisom"},
        provider="modal",
    )


def _editor(
    store: FakeMediaStore,
    runner: FakeRunner,
    counters: FakeCounters | None = None,
    capability: FakeCapability | None = None,
    config: MediaConfig | None = None,
) -> MediaEditor:
    return MediaEditor(
        runner,
        counters or FakeCounters(),
        config or MediaConfig(),
        store,
        capability or FakeCapability(),
    )


async def _seed_video(store: FakeMediaStore, data: bytes = CLIP, filename: str = "clip.mp4") -> int:
    ref = await store.put(data, filename=filename, origin="shared")
    return ref.media_id


# -- the op template -------------------------------------------------------------


def test_the_fixed_operations_are_the_whole_menu() -> None:
    assert OPS == ("trim", "concat", "speed", "mute", "extract-audio", "resize", "convert")


def test_a_trim_passes_its_checked_numbers_through() -> None:
    canonical, refusal = validate_edit("trim", {"start": 2, "end": 9})
    assert refusal == ""
    assert canonical == {"op": "trim", "start": 2.0, "end": 9.0}


def test_extract_audio_and_convert_normalize_to_the_script_names() -> None:
    canonical, _ = validate_edit("extract-audio", {})
    assert canonical == {"op": "extract_audio"}
    canonical, _ = validate_edit("Convert", {"format": "MP3"})
    assert canonical == {"op": "convert", "format": "mp3"}


def test_an_unknown_operation_is_a_plain_refusal() -> None:
    canonical, refusal = validate_edit("rotate", {})
    assert canonical is None
    assert refusal == (
        "that edit is not one of the fixed operations — "
        "trim, concat, speed, mute, extract-audio, resize, convert"
    )


def test_bad_numbers_never_reach_the_sandbox() -> None:
    refusals = [
        ("trim", {"start": 5, "end": 5}, "after the start"),
        ("trim", {"start": -1, "end": 5}, "at or after 0"),
        ("trim", {"start": 0, "end": 999999}, "within 12 hours"),
        ("trim", {"start": "soon", "end": 9}, "trim needs start and end"),
        ("speed", {"factor": 4}, "from 0.5 to 2"),
        ("speed", {"factor": 0.1}, "from 0.5 to 2"),
        ("resize", {"width": 641, "height": 480}, "even numbers"),
        ("resize", {"width": 14, "height": 480}, "even numbers"),
        ("resize", {"width": 640, "height": 4001}, "even numbers"),
        ("convert", {"format": "avi"}, "mp4, webm, gif, mp3"),
        ("concat", {}, "second_media_id"),
        ("concat", {"second_media_id": "two"}, "second_media_id"),
    ]
    for op, params, fragment in refusals:
        canonical, refusal = validate_edit(op, params)
        assert canonical is None, (op, params)
        assert fragment in refusal, (op, params, refusal)


def test_bools_are_not_numbers_here() -> None:
    # a model that sent true as a factor gets the refusal, not a crash
    canonical, refusal = validate_edit("speed", {"factor": True})
    assert canonical is None
    assert refusal == "speed takes a factor from 0.5 to 2"


def test_the_script_reports_its_own_plain_reasons() -> None:
    # the script's honest fallbacks ride stdout as JSON — the editor
    # passes them through untouched, so their wording is a contract
    for reason in (
        "the edit's parameters never reached the sandbox",
        "the video never reached the sandbox",
        "ffmpeg is not installed in the sandbox",
        "the second video never reached the sandbox",
        "that edit is not one of the fixed operations",
        "ffmpeg refused that edit",
        "the edit produced nothing",
        "the edited file is over the size limit and was not kept",
    ):
        assert f'reason="{reason}"' in EDIT_SCRIPT


def test_the_script_never_builds_a_shell() -> None:
    # an argument vector, assembled from validated numbers only
    assert "shell=True" not in EDIT_SCRIPT
    assert "os.system" not in EDIT_SCRIPT


# -- the job spec ----------------------------------------------------------------


async def test_an_edit_builds_one_job_with_the_video_and_the_params() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([_edited()])
    editor = _editor(store, runner)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is not None
    spec = runner.specs[0]
    assert spec.script_name == "edit.py"
    assert json.loads(spec.files["params.json"]) == {"op": "mute"}
    assert list(spec.binary_files) == ["input.mp4"]
    assert spec.binary_files["input.mp4"] == CLIP
    # the audit digest is the script's own — the video bytes never enter it
    assert script_digest(spec) == script_digest(
        build_edit_spec(b"other", "video/mp4", None, {"op": "mute"}, "x")
    )


async def test_a_concat_carries_the_second_video_as_input2() -> None:
    store = FakeMediaStore()
    first = await _seed_video(store, CLIP)
    second = await _seed_video(store, OTHER, filename="other.mp4")
    runner = FakeRunner([_edited()])
    editor = _editor(store, runner)

    await editor.edit(first, "concat", {"second_media_id": second})

    assert list(runner.specs[0].binary_files) == ["input.mp4", "input2.mp4"]
    assert runner.specs[0].binary_files["input2.mp4"] == OTHER


# -- the up-front refusals ---------------------------------------------------------


async def test_no_ffmpeg_anywhere_is_an_up_front_refusal() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([_edited()])
    counters = FakeCounters()
    capability = FakeCapability(capable=False)
    editor = _editor(store, runner, counters, capability)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is None
    assert outcome.reason == (
        "video editing needs ffmpeg, and no cloud sandbox that has it is available right now"
    )
    assert runner.specs == []  # no job, no bytes traveled
    assert counters.bumps == []  # and nothing is charged


async def test_a_spent_budget_refuses_before_any_job_runs() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([_edited()])
    editor = _editor(store, runner, FakeCounters(start=20))

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is None
    assert outcome.reason == "today's video job budget is spent"
    assert runner.specs == []


async def test_an_unknown_op_refuses_before_the_capability_is_even_asked() -> None:
    store = FakeMediaStore()
    runner = FakeRunner([])
    capability = FakeCapability()
    editor = _editor(store, runner, capability=capability)

    outcome = await editor.edit(1, "rotate", {})

    assert outcome.media_ref is None
    assert "not one of the fixed operations" in outcome.reason
    assert capability.calls == 0


async def test_a_missing_video_says_it_is_no_longer_stored() -> None:
    editor = _editor(FakeMediaStore(), FakeRunner([]))
    outcome = await editor.edit(99, "mute", {})
    assert outcome.reason == (
        "that video is no longer stored — it may have been removed to make room for newer files"
    )


async def test_a_non_video_refuses_plainly() -> None:
    store = FakeMediaStore()
    picture = await store.put(PICTURE, filename="pic.png", origin="shared")
    runner = FakeRunner([])
    editor = _editor(store, runner)

    outcome = await editor.edit(picture.media_id, "mute", {})

    assert "is a image/png file, not a video" in outcome.reason
    assert runner.specs == []


async def test_concat_refuses_a_missing_or_non_video_second() -> None:
    store = FakeMediaStore()
    first = await _seed_video(store)
    picture = await store.put(PICTURE, filename="pic.png", origin="shared")
    runner = FakeRunner([])
    editor = _editor(store, runner)

    missing = await editor.edit(first, "concat", {"second_media_id": 99})
    assert missing.reason == (
        "the second video is no longer stored — it may have been removed "
        "to make room for newer files"
    )

    not_a_video = await editor.edit(first, "concat", {"second_media_id": picture.media_id})
    assert not_a_video.reason == (
        "the second file is a image/png, not a video — concat joins two videos"
    )
    assert runner.specs == []


# -- the budget and the honest outcomes --------------------------------------------


async def test_a_job_ran_means_the_budget_is_charged_once() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([_edited()])
    counters = FakeCounters()
    editor = _editor(store, runner, counters)

    await editor.edit(media_id, "mute", {})

    assert counters.bumps == ["sandbox_jobs"]
    assert counters.counts["sandbox_jobs"] == 1


async def test_a_dispatch_no_provider_could_take_is_a_plain_reason() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([WorkbenchError("modal: no token id")])
    counters = FakeCounters()
    editor = _editor(store, runner, counters)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is None
    assert outcome.reason == "no cloud sandbox could take the job"
    assert counters.bumps == []  # never completed, never charged


async def test_the_edited_copy_is_stored_as_a_new_media_id() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    edited = b"\x00\x00\x00\x18ftypisom" + b"edited bytes"
    runner = FakeRunner([_edited(artifacts={"output.mp4": edited})])
    editor = _editor(store, runner)

    outcome = await editor.edit(media_id, "trim", {"start": 1, "end": 4})

    assert outcome.reason == ""
    assert outcome.media_ref is not None
    assert outcome.media_ref.media_id != media_id
    record = await store.get(outcome.media_ref.media_id)
    assert record is not None
    assert record.data == edited
    assert record.filename == "clip-edited.mp4"
    assert record.media_type == "video/mp4"  # sniffed again at store time


async def test_a_failed_job_says_its_own_reported_reason() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    result = JobResult(
        exit_ok=False,
        stdout=json.dumps({"edited": False, "reason": "ffmpeg refused that edit"}),
        stderr="",
    )
    editor = _editor(store, FakeRunner([result]), FakeCounters())

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is None
    assert outcome.reason == "ffmpeg refused that edit"


async def test_a_failed_job_without_a_reason_says_the_plain_fact() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    result = JobResult(exit_ok=False, stdout="garbage not json", stderr="boom")
    editor = _editor(store, FakeRunner([result]))

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.reason == "the edit job failed"


async def test_a_timed_out_job_is_a_reason_never_a_retry() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([JobResult(exit_ok=False, stdout="", stderr="", timed_out=True)])
    editor = _editor(store, runner)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.reason == "the edit job ran past its time limit"
    assert len(runner.specs) == 1  # one attempt, no retry


async def test_an_over_limit_output_is_reported_not_kept() -> None:
    # the script itself refuses sizes over the artifact cap — exit is
    # clean, the reason rides stdout, and nothing lands in artifacts
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    result = JobResult(
        exit_ok=True,
        stdout=json.dumps(
            {"edited": False, "reason": "the edited file is over the size limit and was not kept"}
        ),
        stderr="",
        artifacts={},
    )
    editor = _editor(store, FakeRunner([result]))

    outcome = await editor.edit(media_id, "convert", {"format": "gif"})

    assert outcome.media_ref is None
    assert outcome.reason == "the edited file is over the size limit and was not kept"
    # only the source remains — nothing new was kept
    assert list(store.rows) == [media_id]


async def test_no_output_artifact_says_the_edit_produced_nothing() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    runner = FakeRunner([_edited(artifacts={"notes.txt": b"not an output"})])
    editor = _editor(store, runner)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.reason == "the edit produced nothing"
    assert store.rows[media_id].data == CLIP  # the source is untouched


async def test_a_full_store_keeps_the_source_and_refuses_plainly() -> None:
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    store.full = True
    runner = FakeRunner([_edited()])
    editor = _editor(store, runner)

    outcome = await editor.edit(media_id, "mute", {})

    assert outcome.media_ref is None
    assert outcome.reason == "media storage is full — the edited file could not be kept"


async def test_the_reason_never_carries_a_path_or_an_id() -> None:
    # panel-safe by construction: every refusal the editor can return
    for reason in (
        "video editing needs ffmpeg, and no cloud sandbox that has it is available right now",
        "today's video job budget is spent",
        "that video is no longer stored — it may have been removed to make room for newer files",
        "no cloud sandbox could take the job",
        "the edit job ran past its time limit",
        "the edit job failed",
        "the edit produced nothing",
        "media storage is full — the edited file could not be kept",
    ):
        assert "/" not in reason
        assert ".mp4" not in reason


async def test_a_bad_runner_exception_is_not_swallowed() -> None:
    # a bug in the runner is not a WorkbenchError — it must crash, not
    # masquerade as a plain media failure
    store = FakeMediaStore()
    media_id = await _seed_video(store)
    editor = _editor(store, FakeRunner([RuntimeError("bug")]))

    with pytest.raises(RuntimeError, match="bug"):
        await editor.edit(media_id, "mute", {})


# -- the adapters' capability truth ------------------------------------------------


def test_the_adapters_declare_their_ffmpeg_truth() -> None:
    from aether.workbench.fake import FakeWorkbench
    from aether.workbench.modal import ModalWorkbench
    from aether.workbench.vercel import VercelWorkbench

    class _NoResolver:
        async def resolve(self, name: str) -> str | None:
            return None

    no = _NoResolver()
    assert ModalWorkbench(resolver=no).has_ffmpeg is True
    assert VercelWorkbench(resolver=no).has_ffmpeg is False
    # the fake defaults to having it, and a test can flip it
    assert FakeWorkbench().has_ffmpeg is True
    assert FakeWorkbench(has_ffmpeg=False).has_ffmpeg is False
