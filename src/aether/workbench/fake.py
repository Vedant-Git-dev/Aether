"""A fake workbench — the adapter contract with no sandbox behind it.

Tests use it two ways: as a stand-in provider when routing is the thing
under test (hand it whatever `available`/`accepts` answers the case
needs), and as a probe for the cleanup guarantee (its `cleanups` list
proves the finally ran even when a job blew up mid-run). It never runs
anything — a script's text is only ever read, never executed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from .base import JobResult, JobSpec


class FakeWorkbench:
    """An in-memory provider. `available` and `accept` are whatever the
    test needs (a bool, or a callable for per-spec answers); `result` is
    returned per job, or `handler` computes one and may raise — a raise
    exercises the caller's failure paths and still counts a cleanup."""

    def __init__(
        self,
        *,
        name: str = "fake",
        available: bool | Callable[[], bool] = True,
        accept: bool | Callable[[JobSpec], bool] = True,
        result: JobResult | None = None,
        handler: Callable[[JobSpec], Awaitable[JobResult]] | None = None,
        has_ffmpeg: bool = True,
    ) -> None:
        self.name = name
        # a plain attribute, satisfying the protocol's property member the
        # same way `name` does — tests flip it to exercise the editor's
        # up-front refusal
        self.has_ffmpeg = has_ffmpeg
        self._available = available
        self._accept = accept
        self._result = result
        self._handler = handler
        self.jobs: list[JobSpec] = []
        self.cleanups: list[JobSpec] = []

    async def available(self) -> bool:
        value = self._available
        return value() if callable(value) else bool(value)

    async def accepts(self, spec: JobSpec) -> bool:
        value = self._accept
        return value(spec) if callable(value) else bool(value)

    async def run_job(self, spec: JobSpec) -> JobResult:
        self.jobs.append(spec)
        try:
            if self._handler is not None:
                return await self._handler(spec)
            if self._result is not None:
                # a canned result carries the provider that returned it
                return replace(self._result, provider=self.name)
            return JobResult(
                exit_ok=True,
                stdout=f"ran {spec.script_name} on {self.name}",
                stderr="",
                duration=0.0,
                provider=self.name,
            )
        finally:
            # the guarantee every real adapter must keep: cleanup happens
            # whatever happened inside the job
            self.cleanups.append(spec)
