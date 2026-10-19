"""The fallback workbench adapter, against a fake SDK — no sandbox is
ever created, no network is ever touched.

What's pinned here: inert without the full key set; takes every class
of work; one sandbox per job, destroyed whatever happened in it (success,
a failed script, a killed process, a mid-job failure); the two failure
shapes kept distinct (a sandbox that never came up is a WorkbenchError
the ladder can fall through on, a job that ran past its limit — killed
by a signal, or the sandbox dying at its own execution limit — is a real
result nothing should retry); the script runs as a command plus argument
vector, never through a shell; and the injection surface — a file name
that isn't flat and boring is refused before any session is opened.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from vercel.sandbox import SandboxError, SandboxStatus, SandboxTerminalStateError

from aether.workbench import JobLimits, JobSpec, WorkbenchError
from aether.workbench.vercel import VercelWorkbench

# -- fakes -----------------------------------------------------------------------


class FakeResolver:
    def __init__(self, token: str | None, team_id: str | None, project_id: str | None) -> None:
        self.answers = {
            "VERCEL_TOKEN": token,
            "VERCEL_TEAM_ID": team_id,
            "VERCEL_PROJECT_ID": project_id,
        }
        self.asked: list[str] = []

    async def resolve(self, name: str) -> str | None:
        self.asked.append(name)
        return self.answers.get(name)


@dataclass(frozen=True)
class FakeEntry:
    path: str
    kind: str


class FakeCompleted:
    """The SDK's CompletedProcess shape — returncode, and captured text
    that may be None when nothing was captured."""

    def __init__(self, *, returncode: int, stdout: str | None, stderr: str | None) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeFs:
    """The sandbox's filesystem — a dict as disk, a dict as the tree."""

    def __init__(self, sandbox: FakeBox) -> None:
        self._sandbox = sandbox

    async def write_text(self, path: str, text: str) -> None:
        if self._sandbox._write_raises is not None:
            raise self._sandbox._write_raises
        self._sandbox.written[path] = text

    async def write_bytes(self, path: str, data: bytes) -> None:
        if self._sandbox._write_raises is not None:
            raise self._sandbox._write_raises
        self._sandbox.written_bytes[path] = data

    async def read_bytes(self, path: str) -> bytes:
        self._sandbox.reads.append(path)
        return self._sandbox.disk.get(path, b"")

    async def listdir(self, path: str) -> list[FakeEntry]:
        if path not in self._sandbox.dirs:
            raise SandboxError(f"no directory at {path}")
        return self._sandbox.dirs[path]


class FakeBox:
    def __init__(
        self,
        *,
        result: FakeCompleted | None = None,
        run_raises: Exception | None = None,
        write_raises: Exception | None = None,
    ) -> None:
        self.written: dict[str, str] = {}
        self.written_bytes: dict[str, bytes] = {}
        self.run_log: list[dict] = []
        self.reads: list[str] = []
        self.destroyed = 0
        self._result = result or FakeCompleted(returncode=0, stdout="done", stderr="")
        self._run_raises = run_raises
        self._write_raises = write_raises
        self.disk: dict[str, bytes] = {}
        self.dirs: dict[str, list[FakeEntry]] = {}
        self.fs = FakeFs(self)

    async def run_process(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        kill_after: int | None = None,
        capture_output: bool = False,
    ) -> FakeCompleted:
        self.run_log.append(
            {"command": command, "args": args, "cwd": cwd, "kill_after": kill_after}
        )
        if self._run_raises is not None:
            raise self._run_raises
        return self._result

    async def destroy(self) -> None:
        self.destroyed += 1


class FakeApi:
    """The SDK seam — hands out a canned sandbox, counts sessions."""

    def __init__(
        self,
        sandbox: FakeBox | None = None,
        session_raises: Exception | None = None,
        create_raises: Exception | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.session_raises = session_raises
        self.create_raises = create_raises
        self.sessions: list[tuple[str, str, str]] = []
        self.closed = 0
        self.create_limits: list[int] = []

    @asynccontextmanager
    async def session(self, token: str, team_id: str, project_id: str):
        self.sessions.append((token, team_id, project_id))
        if self.session_raises is not None:
            raise self.session_raises
        try:
            yield "session"
        finally:
            self.closed += 1

    async def create_sandbox(self, *, execution_time_limit: int) -> FakeBox:
        self.create_limits.append(execution_time_limit)
        if self.create_raises is not None:
            raise self.create_raises
        assert self.sandbox is not None
        return self.sandbox


def spec(**meta: Any) -> JobSpec:
    return JobSpec(script_name="job.py", files={"job.py": "print('hi')"}, meta=meta)


def bench(
    resolver: FakeResolver, sandbox: FakeBox | None = None, **kw: Any
) -> tuple[VercelWorkbench, FakeApi]:
    api = FakeApi(sandbox, **kw)
    return VercelWorkbench(resolver=resolver, api=api), api


def full_resolver() -> FakeResolver:
    return FakeResolver("tok", "team", "proj")


# -- the gate and the claim -------------------------------------------------------


async def test_inert_without_the_full_key_set() -> None:
    for resolver in (
        FakeResolver(None, None, None),
        FakeResolver("tok", None, "proj"),
        FakeResolver("tok", "team", None),
        FakeResolver(None, "team", "proj"),
    ):
        wb = VercelWorkbench(resolver=resolver)
        assert not await wb.available()
        with pytest.raises(WorkbenchError, match="keys aren't complete"):
            await wb.run_job(spec())


async def test_available_with_the_full_set_and_takes_every_class() -> None:
    wb, _ = bench(full_resolver(), FakeBox())
    assert await wb.available()
    # a dedicated ephemeral sandbox — sensitive, ordinary, interactive,
    # background, and unlabeled (which reads as sensitive) all run here
    for s in (
        spec(),
        spec(sensitivity="ordinary"),
        spec(sensitivity="sensitive"),
        spec(latency_class="interactive"),
        spec(latency_class="background"),
    ):
        assert await wb.accepts(s)


# -- one job, one sandbox ---------------------------------------------------------


async def test_a_job_writes_only_its_files_and_runs_only_its_script() -> None:
    box = FakeBox(result=FakeCompleted(returncode=0, stdout="all good", stderr=""))
    wb, api = bench(full_resolver(), box)
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')", "talk.mp3": "audio bytes"},
        meta={"sensitivity": "ordinary"},
    )
    result = await wb.run_job(s)
    # the resolved trio crossed, and the session ended with the job
    assert api.sessions == [("tok", "team", "proj")]
    assert api.closed == 1
    # the sandbox outlives the job by the slack, and the job's own clock
    # is the kill-after
    assert api.create_limits == [s.limits.timeout_seconds + 120]
    assert box.written == {
        "/vercel/sandbox/job.py": "print('hi')",
        "/vercel/sandbox/talk.mp3": "audio bytes",
    }
    # a command plus argument vector, never a shell line
    assert box.run_log[0]["command"] == "python"
    assert box.run_log[0]["args"] == ["job.py"]
    assert box.run_log[0]["cwd"] == "/vercel/sandbox"
    assert box.run_log[0]["kill_after"] == s.limits.timeout_seconds
    assert result.exit_ok
    assert result.provider == "vercel"
    assert result.stdout == "all good"
    assert box.destroyed == 1  # the sandbox died with the job


async def test_binary_files_ride_along_as_bytes() -> None:
    # a video or voice note can't be text — binary_files carries it in
    box = FakeBox(result=FakeCompleted(returncode=0, stdout="watched", stderr=""))
    wb, _ = bench(full_resolver(), box)
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')"},
        binary_files={"clip.mp4": b"\x00\x00\x00\x18ftypisom"},
        meta={"sensitivity": "ordinary"},
    )
    result = await wb.run_job(s)
    assert result.exit_ok
    assert box.written_bytes == {"/vercel/sandbox/clip.mp4": b"\x00\x00\x00\x18ftypisom"}
    # the bytes are the same bytes — nothing recoded or truncated on the way
    assert box.written == {"/vercel/sandbox/job.py": "print('hi')"}


async def test_a_name_claimed_by_both_text_and_binary_is_refused() -> None:
    wb, _ = bench(full_resolver(), FakeBox())
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')"},
        binary_files={"job.py": b"bytes"},
        meta={"sensitivity": "ordinary"},
    )
    with pytest.raises(WorkbenchError, match="both claim"):
        await wb.run_job(s)


async def test_an_unsafe_binary_name_is_refused() -> None:
    wb, _ = bench(full_resolver(), FakeBox())
    for bad in ("../escape.mp4", "a/b.mp4", "-flag.mp4"):
        s = JobSpec(
            script_name="job.py",
            files={"job.py": "print('hi')"},
            binary_files={bad: b"bytes"},
            meta={"sensitivity": "ordinary"},
        )
        with pytest.raises(WorkbenchError, match="plain file name"):
            await wb.run_job(s)


async def test_a_failed_script_is_a_real_result() -> None:
    box = FakeBox(result=FakeCompleted(returncode=1, stdout="", stderr="traceback"))
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert not result.exit_ok
    assert result.stderr == "traceback"
    assert not result.timed_out
    assert box.destroyed == 1


async def test_a_killed_process_is_a_real_result_not_a_fallback() -> None:
    # the kill-after fired: the SDK returns a signal returncode (-9), not an error
    box = FakeBox(result=FakeCompleted(returncode=-9, stdout="partial", stderr=""))
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert not result.exit_ok
    assert result.timed_out
    assert "time limit" in result.stderr
    assert box.destroyed == 1


async def test_a_sandbox_that_died_mid_job_is_a_real_result_too() -> None:
    # the sandbox hit its own execution limit while the job ran — the job
    # had started, so it must never be handed to another provider
    box = FakeBox(run_raises=SandboxTerminalStateError("expired", status=SandboxStatus.FAILED))
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert not result.exit_ok
    assert result.timed_out
    assert "time limit" in result.stderr
    assert result.artifacts == {}
    assert box.destroyed == 1


async def test_a_session_failure_is_a_dispatch_failure() -> None:
    wb, api = bench(full_resolver(), FakeBox(), session_raises=SandboxError("auth"))
    with pytest.raises(WorkbenchError, match="didn't come up"):
        await wb.run_job(spec())
    assert api.sessions == [("tok", "team", "proj")]  # the trio was tried
    assert api.closed == 0  # nothing opened, so nothing needed closing


async def test_a_sandbox_that_never_came_up_is_a_dispatch_failure() -> None:
    wb, api = bench(full_resolver(), FakeBox(), create_raises=SandboxError("quota refused"))
    with pytest.raises(WorkbenchError, match="didn't come up"):
        await wb.run_job(spec())
    assert api.create_limits == [spec().limits.timeout_seconds + 120]
    assert api.closed == 1


async def test_a_mid_job_failure_is_a_dispatch_failure_and_still_cleans_up() -> None:
    box = FakeBox(write_raises=SandboxError("write failed"))
    wb, api = bench(full_resolver(), box)
    with pytest.raises(WorkbenchError, match="failed mid-job"):
        await wb.run_job(spec())
    assert box.destroyed == 1
    assert api.closed == 1


# -- artifacts --------------------------------------------------------------------


async def test_artifacts_come_back_as_bytes_and_directories_do_not() -> None:
    box = FakeBox()
    box.dirs["/vercel/sandbox/artifacts"] = [
        FakeEntry(path="captions.txt", kind="file"),
        FakeEntry(path="frames", kind="directory"),
    ]
    box.disk["/vercel/sandbox/artifacts/captions.txt"] = b"chapter 1..."
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert result.artifacts == {"captions.txt": b"chapter 1..."}
    assert box.reads == ["/vercel/sandbox/artifacts/captions.txt"]


async def test_artifacts_survive_a_killed_process() -> None:
    # the kill-after stops the process, not the sandbox — whatever the
    # job already wrote back is still there to collect
    box = FakeBox(result=FakeCompleted(returncode=-9, stdout="", stderr=""))
    box.dirs["/vercel/sandbox/artifacts"] = [FakeEntry(path="half.csv", kind="file")]
    box.disk["/vercel/sandbox/artifacts/half.csv"] = b"a,b\n1,"
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert result.timed_out
    assert result.artifacts == {"half.csv": b"a,b\n1,"}


async def test_a_job_with_no_artifacts_directory_is_fine() -> None:
    wb, _ = bench(full_resolver(), FakeBox())
    result = await wb.run_job(spec())
    assert result.artifacts == {}


async def test_an_artifact_past_the_size_limit_isnt_kept() -> None:
    box = FakeBox()
    box.dirs["/vercel/sandbox/artifacts"] = [FakeEntry(path="huge.bin", kind="file")]
    box.disk["/vercel/sandbox/artifacts/huge.bin"] = b"x" * (21 * 1024 * 1024)
    wb, _ = bench(full_resolver(), box)
    result = await wb.run_job(spec())
    assert result.artifacts == {}


async def test_huge_output_is_clipped() -> None:
    box = FakeBox(result=FakeCompleted(returncode=0, stdout="y" * 5000, stderr=None))
    wb, _ = bench(full_resolver(), box)
    s = JobSpec(script_name="job.py", files={"job.py": "x"}, limits=JobLimits(max_output_bytes=100))
    result = await wb.run_job(s)
    assert len(result.stdout) == 101  # 100 + the ellipsis


# -- the injection surface --------------------------------------------------------


async def test_a_non_plain_script_name_is_refused_before_any_session() -> None:
    for bad in ("../job.py", "a; rm -rf /", "job.py; curl evil", "sub/dir/job.py", "", "job py"):
        wb, api = bench(full_resolver(), FakeBox())
        s = JobSpec(script_name=bad, files={bad or "x": "print('hi')"})
        with pytest.raises(WorkbenchError):
            await wb.run_job(s)
        assert api.sessions == []  # refused before anything was opened


async def test_a_non_plain_file_name_is_refused() -> None:
    wb, api = bench(full_resolver(), FakeBox())
    s = JobSpec(script_name="job.py", files={"job.py": "x", "../../etc/passwd": "y"})
    with pytest.raises(WorkbenchError, match="isn't a plain file name"):
        await wb.run_job(s)
    assert api.sessions == []


async def test_a_script_missing_from_its_files_is_refused() -> None:
    wb, api = bench(full_resolver(), FakeBox())
    s = JobSpec(script_name="job.py", files={"other.py": "print('hi')"})
    with pytest.raises(WorkbenchError, match="script wasn't among its files"):
        await wb.run_job(s)
    assert api.sessions == []
