"""The fallback workbench adapter — one throwaway sandbox per job, on a
second cloud.

Same shape as the default adapter: one job gets one fresh sandbox,
destroyed after it, nothing shared and nothing of Aether's on board.
It exists so the ladder has somewhere to fall through to when the
default cloud is out of quota — the model still never routes; this
adapter simply takes whatever the default one takes, because a
dedicated ephemeral sandbox is the same trust class wherever it runs.

The script runs as a command plus argument vector — `("python",
["job.py"])` — never through a shell, and the job's own clock is the
SDK's kill-after: a process that runs past its limit is killed with a
signal and comes back as a real result, so nothing is ever run twice
by a well-meaning retry.

Everything here fails in the two shapes the ladder understands: a
dispatch that never produced a sandbox raises WorkbenchError (the
caller falls through to another provider), while a job that ran and
failed — killed at its limit, exited nonzero — comes back as a real
result. The one Vercel-specific wrinkle: a sandbox that dies at its
own execution limit mid-job surfaces as a terminal-state error, which
reads as a real result too — the job had started, so it is never
handed to another provider.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from vercel import sandbox as vercel_sandbox
from vercel.api import session as vercel_session
from vercel.sandbox import (
    SandboxCredentials,
    SandboxError,
    SandboxServiceOptions,
    SandboxTerminalStateError,
)

from .base import JobResult, JobSpec, KeyResolver, WorkbenchError

log = logging.getLogger("aether.workbench.vercel")

_TOKEN = "VERCEL_TOKEN"
_TEAM_ID = "VERCEL_TEAM_ID"
_PROJECT_ID = "VERCEL_PROJECT_ID"
_WORKDIR = "/vercel/sandbox"
_ARTIFACTS = "artifacts"
# the sandbox outlives the job by the slack, so a job killed at its own
# limit still leaves time to collect artifacts before the clock kills it
_LIFETIME_SLACK = 120
# one artifact may be at most this big — a captions file is a few KB; a
# job trying to drag a whole dataset back is skipped, not crashed on
_ARTIFACT_LIMIT = 20 * 1024 * 1024
# a flat file name — no paths, no shell metacharacters, nothing that
# could climb out of the job's directory
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class VercelApi(Protocol):
    """The SDK seam — open a credentialed session, make a sandbox — so
    no test ever talks to the sandbox service. `session` owns the
    credential scope: everything created inside it answers to the same
    token and ids."""

    def session(
        self, token: str, team_id: str, project_id: str
    ) -> AbstractAsyncContextManager[Any]: ...

    async def create_sandbox(self, *, execution_time_limit: int) -> Any: ...


class _LiveVercelApi:
    """The thin, real SDK behind the seam."""

    @asynccontextmanager
    async def session(self, token: str, team_id: str, project_id: str) -> AsyncGenerator[Any]:
        async def factory() -> SandboxCredentials:
            return SandboxCredentials(token=token, team_id=team_id, project_id=project_id)

        options = SandboxServiceOptions(credentials_factory=factory)
        async with vercel_session(service_options=[options]):
            yield

    async def create_sandbox(self, *, execution_time_limit: int) -> Any:
        return await vercel_sandbox.create_sandbox(execution_time_limit=execution_time_limit)


def _validate(spec: JobSpec) -> None:
    """Names a spec may carry: flat, plain, and boring. The script name
    reaches a command plus argument vector, so nothing here could
    inject anyway — but a boring name is still required, so a spec
    can't smuggle a path or a flag into the job's world."""
    if not _SAFE_NAME.fullmatch(spec.script_name):
        raise WorkbenchError(
            f"the job's script name ({spec.script_name!r}) isn't a plain file name — not run."
        )
    if spec.script_name not in spec.files:
        raise WorkbenchError("the job's script wasn't among its files — not run.")
    for name in spec.files:
        if not _SAFE_NAME.fullmatch(name):
            raise WorkbenchError(f"a job file ({name!r}) isn't a plain file name — not run.")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class VercelWorkbench:
    """The fallback provider. Inert without its full key set; takes
    every class of work; one sandbox per job, destroyed in finally
    whatever happened."""

    def __init__(self, *, resolver: KeyResolver, api: VercelApi | None = None) -> None:
        self._resolver = resolver
        self._api: VercelApi = api or _LiveVercelApi()

    @property
    def name(self) -> str:
        return "vercel"

    async def available(self) -> bool:
        return bool(
            await self._resolver.resolve(_TOKEN)
            and await self._resolver.resolve(_TEAM_ID)
            and await self._resolver.resolve(_PROJECT_ID)
        )

    async def accepts(self, spec: JobSpec) -> bool:
        # a dedicated ephemeral sandbox takes every class of work —
        # same trust class as the default provider
        del spec
        return True

    async def run_job(self, spec: JobSpec) -> JobResult:
        token = await self._resolver.resolve(_TOKEN)
        team_id = await self._resolver.resolve(_TEAM_ID)
        project_id = await self._resolver.resolve(_PROJECT_ID)
        if not (token and team_id and project_id):
            raise WorkbenchError(
                "the sandbox keys aren't complete — the key and the team and project "
                "ids can be pasted on the settings page."
            )
        _validate(spec)
        started = time.monotonic()
        box: Any = None
        try:
            try:
                # the credential scope lives for the job and ends with it
                async with self._api.session(token, team_id, project_id):
                    try:
                        box = await self._api.create_sandbox(
                            execution_time_limit=spec.limits.timeout_seconds + _LIFETIME_SLACK
                        )
                    except SandboxError as exc:
                        raise WorkbenchError(
                            f"the cloud sandbox didn't come up — {type(exc).__name__}."
                        ) from exc
                    try:
                        for name, content in spec.files.items():
                            await box.fs.write_text(f"{_WORKDIR}/{name}", content)
                        result = await box.run_process(
                            "python",
                            [spec.script_name],
                            cwd=_WORKDIR,
                            kill_after=spec.limits.timeout_seconds,
                            capture_output=True,
                        )
                    except SandboxTerminalStateError as exc:
                        # the sandbox died at its own limit mid-job — the
                        # job had started, so this is a real result and
                        # never a fallback
                        raise _TimedOut() from exc
                    except SandboxError as exc:
                        raise WorkbenchError(
                            f"the cloud sandbox failed mid-job — {type(exc).__name__}."
                        ) from exc
                    artifacts = await self._collect_artifacts(box)
                    if result.returncode < 0:
                        # killed by a signal at its own limit — a real
                        # result, never a fallback (the job already happened)
                        return JobResult(
                            exit_ok=False,
                            stdout=_clip(result.stdout or "", spec.limits.max_output_bytes),
                            stderr="the job ran past its time limit and was stopped.",
                            artifacts=artifacts,
                            duration=time.monotonic() - started,
                            provider=self.name,
                            timed_out=True,
                        )
                    return JobResult(
                        exit_ok=result.returncode == 0,
                        stdout=_clip(result.stdout or "", spec.limits.max_output_bytes),
                        stderr=_clip(result.stderr or "", spec.limits.max_output_bytes),
                        artifacts=artifacts,
                        duration=time.monotonic() - started,
                        provider=self.name,
                    )
            except _TimedOut:
                return JobResult(
                    exit_ok=False,
                    stdout="",
                    stderr="the job ran past its time limit and was stopped.",
                    artifacts={},
                    duration=time.monotonic() - started,
                    provider=self.name,
                    timed_out=True,
                )
            except SandboxError as exc:
                # the session itself never opened — a dispatch failure,
                # same shape as a sandbox that didn't come up
                raise WorkbenchError(
                    f"the cloud sandbox didn't come up — {type(exc).__name__}."
                ) from exc
        finally:
            if box is not None:
                # the guarantee: the sandbox dies whatever happened in it
                try:
                    await box.destroy()
                except Exception:
                    log.warning("a sandbox couldn't be cleaned up", exc_info=True)

    async def _collect_artifacts(self, box: Any) -> dict[str, bytes]:
        """Whatever the job left in its artifacts directory, as bytes —
        the only files that ever come back. A job that wrote nothing
        back, or named a directory, simply returns fewer files."""
        out: dict[str, bytes] = {}
        try:
            entries = await box.fs.listdir(f"{_WORKDIR}/{_ARTIFACTS}")
        except Exception:
            return out  # no artifacts directory — nothing to collect
        for entry in entries:
            if entry.kind != "file":
                continue
            try:
                data = await box.fs.read_bytes(f"{_WORKDIR}/{_ARTIFACTS}/{entry.path}")
            except Exception:
                log.debug("an artifact couldn't be read back", exc_info=True)
                continue
            if len(data) > _ARTIFACT_LIMIT:
                log.info("an artifact was over the size limit and wasn't kept")
                continue
            out[entry.path] = data
        return out


class _TimedOut(Exception):
    """Internal: the sandbox died at its own execution limit mid-job —
    unwound to a real result instead of a dispatch failure."""
