"""The default workbench adapter, against a fake SDK — no sandbox is
ever created, no network is ever touched.

What's pinned here: inert without the key pair; takes every class of
work; one sandbox per job, terminated whatever happened in it (success,
a failed script, a blown time limit, a mid-job failure); the two failure
shapes kept distinct (a sandbox that never came up is a WorkbenchError
the ladder can fall through on, a job that ran past its limit is a real
result nothing should retry); the script runs as an argument vector,
never through a shell; and the injection surface — a file name that
isn't flat and boring is refused before any connection is opened.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from modal.exception import Error as ModalError
from modal.exception import ExecTimeoutError, SandboxTimeoutError
from modal.types import FileInfo, FileType

from aether.workbench import JobLimits, JobSpec, WorkbenchError
from aether.workbench.modal import ModalWorkbench

# -- fakes ----------------------------------------------------------------------


class FakeResolver:
    def __init__(self, token_id: str | None, token_secret: str | None) -> None:
        self.answers = {"MODAL_TOKEN_ID": token_id, "MODAL_TOKEN_SECRET": token_secret}
        self.asked: list[str] = []

    async def resolve(self, name: str) -> str | None:
        self.asked.append(name)
        return self.answers.get(name)


class FakeCall:
    """One call the adapter makes, in the `.aio` shape the SDK wraps
    everything in — `x.m.aio(...)` awaits the same fake."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._fn(*args, **kwargs)

    @property
    def aio(self) -> FakeCall:
        return self


class FakeStream:
    def __init__(self, text: str) -> None:
        self.text = text
        self.read = FakeCall(self._read)

    async def _read(self) -> str:
        return self.text


class FakeProc:
    def __init__(self, *, code: int, stdout: str, stderr: str) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.wait = FakeCall(self._wait)
        self._code = code

    async def _wait(self) -> int:
        return self._code


def entry(name: str, kind: FileType = FileType.FILE, size: int = 10) -> FileInfo:
    return FileInfo(
        name=name,
        path=f"/root/artifacts/{name}",
        type=kind,
        size=size,
        mode=0,
        permissions="-rw-r--r--",
        owner="root",
        group="root",
        modified_time=0.0,
        symlink_target=None,
    )


class FakeFs:
    """The sandbox's filesystem — a dict as disk, a dict as the tree."""

    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox
        self.write_text = FakeCall(self._write_text)
        self.write_bytes = FakeCall(self._write_bytes)
        self.read_bytes = FakeCall(self._read_bytes)
        self.list_files = FakeCall(self._list_files)

    async def _write_text(self, data: str, remote_path: str) -> None:
        if self._sandbox._write_raises is not None:
            raise self._sandbox._write_raises
        self._sandbox.written[remote_path] = data

    async def _write_bytes(self, data: bytes, remote_path: str) -> None:
        # modal's write_bytes takes (data, path) — the reversed order the
        # SDK itself uses
        if self._sandbox._write_raises is not None:
            raise self._sandbox._write_raises
        self._sandbox.written_bytes[remote_path] = data

    async def _read_bytes(self, remote_path: str) -> bytes:
        self._sandbox.reads.append(remote_path)
        return self._sandbox.disk.get(remote_path, b"")

    async def _list_files(self, remote_path: str) -> list[FileInfo]:
        if remote_path not in self._sandbox.dirs:
            raise ModalError(f"no directory at {remote_path}")
        return self._sandbox.dirs[remote_path]


class FakeSandbox:
    def __init__(
        self,
        *,
        proc: FakeProc | None = None,
        exec_raises: Exception | None = None,
        write_raises: Exception | None = None,
    ) -> None:
        self.written: dict[str, str] = {}
        self.written_bytes: dict[str, bytes] = {}
        self.exec_log: list[dict] = []
        self.reads: list[str] = []
        self.terminated = 0
        self._proc = proc or FakeProc(code=0, stdout="done", stderr="")
        self._exec_raises = exec_raises
        self._write_raises = write_raises
        self.disk: dict[str, bytes] = {}
        self.dirs: dict[str, list[FileInfo]] = {}
        self.filesystem = FakeFs(self)
        self.terminate = FakeCall(self._terminate)
        self.exec = FakeCall(self._exec)

    async def _terminate(self) -> None:
        self.terminated += 1

    async def _exec(
        self, *argv: str, timeout: int | None = None, workdir: str | None = None
    ) -> FakeProc:
        self.exec_log.append({"argv": argv, "timeout": timeout, "workdir": workdir})
        if self._exec_raises is not None:
            raise self._exec_raises
        return self._proc


class FakeApi:
    """The SDK seam — hands out a canned sandbox, counts connections."""

    def __init__(
        self,
        sandbox: FakeSandbox | None = None,
        connect_raises: Exception | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.connect_raises = connect_raises
        self.connections: list[tuple[str, str]] = []
        self.closed = 0
        self.app_lookups = 0
        self.create_opts: dict[str, Any] | None = None

    @asynccontextmanager
    async def connection(self, token_id: str, token_secret: str):
        self.connections.append((token_id, token_secret))
        if self.connect_raises is not None:
            raise self.connect_raises
        try:
            yield f"client-{len(self.connections)}"
        finally:
            self.closed += 1

    async def lookup_app(self, client: Any) -> str:
        self.app_lookups += 1
        return f"app-for-{client}"

    async def create_sandbox(self, app: str, *, timeout: int, client: Any) -> FakeSandbox:
        self.create_opts = {"app": app, "timeout": timeout, "client": client}
        assert self.sandbox is not None
        return self.sandbox


def spec(**meta: Any) -> JobSpec:
    return JobSpec(script_name="job.py", files={"job.py": "print('hi')"}, meta=meta)


def bench(
    resolver: FakeResolver, sandbox: FakeSandbox | None = None, **kw: Any
) -> tuple[ModalWorkbench, FakeApi]:
    api = FakeApi(sandbox, **kw)
    return ModalWorkbench(resolver=resolver, api=api), api


# -- the gate and the claim ------------------------------------------------------


def test_ffmpeg_is_baked_into_the_image() -> None:
    # the capability the editor asks before any bytes travel
    wb = ModalWorkbench(resolver=FakeResolver("id", "secret"))
    assert wb.has_ffmpeg is True


async def test_inert_without_the_key_pair() -> None:
    for resolver in (
        FakeResolver(None, None),
        FakeResolver("id", None),
        FakeResolver(None, "secret"),
    ):
        wb = ModalWorkbench(resolver=resolver)
        assert not await wb.available()
        with pytest.raises(WorkbenchError, match="key pair isn't set"):
            await wb.run_job(spec())


async def test_available_with_the_pair_and_takes_every_class() -> None:
    wb, _ = bench(FakeResolver("id", "secret"), FakeSandbox())
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


# -- one job, one sandbox --------------------------------------------------------


async def test_a_job_writes_only_its_files_and_runs_only_its_script() -> None:
    sandbox = FakeSandbox(proc=FakeProc(code=0, stdout="all good", stderr=""))
    wb, api = bench(FakeResolver("id", "secret"), sandbox)
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')", "talk.mp3": "audio bytes"},
        meta={"sensitivity": "ordinary"},
    )
    result = await wb.run_job(s)
    # the resolved pair crossed, and the client was closed after the job
    assert api.connections == [("id", "secret")]
    assert api.closed == 1
    # the app is looked up per job, and the sandbox outlives the job by the slack
    assert api.create_opts is not None
    assert api.create_opts["timeout"] == s.limits.timeout_seconds + 120
    assert sandbox.written == {"/root/job.py": "print('hi')", "/root/talk.mp3": "audio bytes"}
    # an argument vector, never a shell line — "python job.py" as two strings
    assert sandbox.exec_log[0]["argv"] == ("python", "job.py")
    assert sandbox.exec_log[0]["workdir"] == "/root"
    assert sandbox.exec_log[0]["timeout"] == s.limits.timeout_seconds
    assert result.exit_ok
    assert result.provider == "modal"
    assert result.stdout == "all good"
    assert sandbox.terminated == 1  # the sandbox died with the job


async def test_binary_files_ride_along_as_bytes() -> None:
    # a video or voice note can't be text — binary_files carries it in
    sandbox = FakeSandbox(proc=FakeProc(code=0, stdout="watched", stderr=""))
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')"},
        binary_files={"clip.mp4": b"\x00\x00\x00\x18ftypisom"},
        meta={"sensitivity": "ordinary"},
    )
    result = await wb.run_job(s)
    assert result.exit_ok
    assert sandbox.written_bytes == {"/root/clip.mp4": b"\x00\x00\x00\x18ftypisom"}
    # the bytes are the same bytes — nothing recoded or truncated on the way
    assert sandbox.written == {"/root/job.py": "print('hi')"}


async def test_a_name_claimed_by_both_text_and_binary_is_refused() -> None:
    wb, _ = bench(FakeResolver("id", "secret"), FakeSandbox())
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')"},
        binary_files={"job.py": b"bytes"},
        meta={"sensitivity": "ordinary"},
    )
    with pytest.raises(WorkbenchError, match="both claim"):
        await wb.run_job(s)


async def test_an_unsafe_binary_name_is_refused() -> None:
    wb, _ = bench(FakeResolver("id", "secret"), FakeSandbox())
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
    sandbox = FakeSandbox(proc=FakeProc(code=1, stdout="", stderr="traceback"))
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    result = await wb.run_job(spec())
    assert not result.exit_ok
    assert result.stderr == "traceback"
    assert not result.timed_out
    assert sandbox.terminated == 1


async def test_a_job_past_its_limit_is_stopped_and_not_retried_elsewhere() -> None:
    for blown in (ExecTimeoutError("exec too slow"), SandboxTimeoutError("sandbox timed out")):
        sandbox = FakeSandbox(exec_raises=blown)
        wb, _ = bench(FakeResolver("id", "secret"), sandbox)
        result = await wb.run_job(spec())
        assert not result.exit_ok
        assert result.timed_out
        assert "time limit" in result.stderr
        assert sandbox.terminated == 1


async def test_a_connection_failure_is_a_dispatch_failure() -> None:
    wb, api = bench(FakeResolver("id", "secret"), FakeSandbox(), connect_raises=ModalError("auth"))
    with pytest.raises(WorkbenchError, match="didn't come up"):
        await wb.run_job(spec())
    assert api.connections == [("id", "secret")]  # the pair was tried
    assert api.closed == 0  # nothing opened, so nothing needed closing


async def test_a_sandbox_that_never_came_up_is_a_dispatch_failure() -> None:
    # a create that raises before any sandbox exists — nothing to clean up
    class FailingApi(FakeApi):
        async def create_sandbox(self, app: str, *, timeout: int, client: Any) -> FakeSandbox:
            raise ModalError("quota refused")

    api = FailingApi()
    wb = ModalWorkbench(resolver=FakeResolver("id", "secret"), api=api)
    with pytest.raises(WorkbenchError, match="didn't come up"):
        await wb.run_job(spec())


async def test_a_mid_job_failure_is_a_dispatch_failure_and_still_cleans_up() -> None:
    sandbox = FakeSandbox(write_raises=ModalError("write failed"))
    wb, api = bench(FakeResolver("id", "secret"), sandbox)
    with pytest.raises(WorkbenchError, match="failed mid-job"):
        await wb.run_job(spec())
    assert sandbox.terminated == 1
    assert api.closed == 1


# -- artifacts -------------------------------------------------------------------


async def test_artifacts_come_back_as_bytes_and_directories_do_not() -> None:
    sandbox = FakeSandbox()
    sandbox.dirs["/root/artifacts"] = [
        entry("captions.txt"),
        entry("frames", kind=FileType.DIRECTORY),
    ]
    sandbox.disk["/root/artifacts/captions.txt"] = b"chapter 1..."
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    result = await wb.run_job(spec())
    assert result.artifacts == {"captions.txt": b"chapter 1..."}
    assert sandbox.reads == ["/root/artifacts/captions.txt"]


async def test_a_job_with_no_artifacts_directory_is_fine() -> None:
    sandbox = FakeSandbox()
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    result = await wb.run_job(spec())
    assert result.artifacts == {}


async def test_an_artifact_past_the_size_limit_isnt_kept() -> None:
    sandbox = FakeSandbox()
    sandbox.dirs["/root/artifacts"] = [entry("huge.bin", size=21 * 1024 * 1024)]
    sandbox.disk["/root/artifacts/huge.bin"] = b"x" * (21 * 1024 * 1024)
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    result = await wb.run_job(spec())
    assert result.artifacts == {}


async def test_huge_output_is_clipped() -> None:
    sandbox = FakeSandbox(proc=FakeProc(code=0, stdout="y" * 5000, stderr=""))
    wb, _ = bench(FakeResolver("id", "secret"), sandbox)
    s = JobSpec(script_name="job.py", files={"job.py": "x"}, limits=JobLimits(max_output_bytes=100))
    result = await wb.run_job(s)
    assert len(result.stdout) == 101  # 100 + the ellipsis


# -- the injection surface -------------------------------------------------------


async def test_a_non_plain_script_name_is_refused_before_any_connection() -> None:
    for bad in ("../job.py", "a; rm -rf /", "job.py; curl evil", "sub/dir/job.py", "", "job py"):
        wb, api = bench(FakeResolver("id", "secret"), FakeSandbox())
        s = JobSpec(script_name=bad, files={bad or "x": "print('hi')"})
        with pytest.raises(WorkbenchError):
            await wb.run_job(s)
        assert api.connections == []  # refused before anything was opened


async def test_a_non_plain_file_name_is_refused() -> None:
    wb, api = bench(FakeResolver("id", "secret"), FakeSandbox())
    s = JobSpec(script_name="job.py", files={"job.py": "x", "../../etc/passwd": "y"})
    with pytest.raises(WorkbenchError, match="isn't a plain file name"):
        await wb.run_job(s)
    assert api.connections == []


async def test_a_script_missing_from_its_files_is_refused() -> None:
    wb, api = bench(FakeResolver("id", "secret"), FakeSandbox())
    s = JobSpec(script_name="job.py", files={"other.py": "print('hi')"})
    with pytest.raises(WorkbenchError, match="script wasn't among its files"):
        await wb.run_job(s)
    assert api.connections == []
