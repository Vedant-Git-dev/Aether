"""The default workbench adapter — one throwaway sandbox per job.

One job gets one fresh sandbox: created for it, terminated after it,
nothing shared with any other job and nothing of Aether's on board. The
keys that cross are the adapter's own, held by the client and never
written into the sandbox — the job sees exactly the files its spec
carries.

Because the sandbox is dedicated and short-lived, this adapter takes
every class of work — sensitive, interactive, background, and unlabeled
(which reads as sensitive anyway). It is the trust class the routing
ladder falls back to; a batch provider refuses what this one accepts.

The script runs as an argument vector — `["python", "job.py"]` — never
through a shell, so a crafted file name has nothing to inject into; the
flat-name check on the spec stays as belt-and-braces.

Everything here fails in the two shapes the ladder understands: a
dispatch that never produced a sandbox raises WorkbenchError (the caller
falls through to another provider), while a job that ran and failed —
blew past its own time limit, exited nonzero — comes back as a real
result, so nothing is ever run twice by a well-meaning retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

import modal
from modal.exception import Error as ModalError
from modal.exception import ExecTimeoutError, SandboxTimeoutError

from .base import JobResult, JobSpec, KeyResolver, WorkbenchError, clip, validate_names

log = logging.getLogger("aether.workbench.modal")

_TOKEN_ID = "MODAL_TOKEN_ID"
_TOKEN_SECRET = "MODAL_TOKEN_SECRET"
_APP_NAME = "aether-workbench"  # one named app groups every sandbox on the dashboard
_WORKDIR = "/root"
_ARTIFACTS = "artifacts"
# the sandbox outlives the job by the slack, so a job at its own limit
# still leaves time to collect artifacts before the clock kills it
_LIFETIME_SLACK = 120
# one artifact may be at most this big — a captions file is a few KB; a
# job trying to drag a whole dataset back is skipped, not crashed on
_ARTIFACT_LIMIT = 20 * 1024 * 1024


class ModalApi(Protocol):
    """The SDK seam — connect, find the app, make a sandbox — so no test
    ever talks to the sandbox service. `connection` owns the client's
    lifetime: it hands one over and closes it when the block ends."""

    def connection(self, token_id: str, token_secret: str) -> AbstractAsyncContextManager[Any]: ...

    async def lookup_app(self, client: Any) -> Any: ...

    async def create_sandbox(self, app: Any, *, timeout: int, client: Any) -> Any: ...


class _LiveModalApi:
    """The thin, real SDK behind the seam."""

    @asynccontextmanager
    async def connection(self, token_id: str, token_secret: str) -> AsyncGenerator[Any]:
        async with await modal.Client.from_credentials.aio(token_id, token_secret) as client:
            yield client

    async def lookup_app(self, client: Any) -> Any:
        return await modal.App.lookup.aio(_APP_NAME, client=client, create_if_missing=True)

    async def create_sandbox(self, app: Any, *, timeout: int, client: Any) -> Any:
        return await modal.Sandbox.create.aio(
            app=app, timeout=timeout, workdir=_WORKDIR, client=client
        )


class ModalWorkbench:
    """The default provider. Inert without its key pair; takes every
    class of work; one sandbox per job, terminated in finally whatever
    happened."""

    def __init__(self, *, resolver: KeyResolver, api: ModalApi | None = None) -> None:
        self._resolver = resolver
        self._api: ModalApi = api or _LiveModalApi()

    @property
    def name(self) -> str:
        return "modal"

    async def available(self) -> bool:
        return bool(
            await self._resolver.resolve(_TOKEN_ID) and await self._resolver.resolve(_TOKEN_SECRET)
        )

    async def accepts(self, spec: JobSpec) -> bool:
        # a dedicated ephemeral sandbox takes every class of work —
        # this is the trust class the ladder falls back to
        del spec
        return True

    async def run_job(self, spec: JobSpec) -> JobResult:
        token_id = await self._resolver.resolve(_TOKEN_ID)
        token_secret = await self._resolver.resolve(_TOKEN_SECRET)
        if not (token_id and token_secret):
            raise WorkbenchError(
                "the sandbox key pair isn't set — both halves can be pasted on the settings page."
            )
        validate_names(spec)
        started = time.monotonic()
        sandbox: Any = None
        try:
            try:
                # the client lives for the job and closes when the block ends
                async with self._api.connection(token_id, token_secret) as client:
                    try:
                        app = await self._api.lookup_app(client)
                        sandbox = await self._api.create_sandbox(
                            app,
                            timeout=spec.limits.timeout_seconds + _LIFETIME_SLACK,
                            client=client,
                        )
                    except ModalError as exc:
                        raise WorkbenchError(
                            f"the cloud sandbox didn't come up — {type(exc).__name__}."
                        ) from exc
                    try:
                        await asyncio.gather(
                            *(
                                sandbox.filesystem.write_text.aio(content, f"{_WORKDIR}/{name}")
                                for name, content in spec.files.items()
                            )
                        )
                        proc = await sandbox.exec.aio(
                            "python",
                            spec.script_name,
                            timeout=spec.limits.timeout_seconds,
                            workdir=_WORKDIR,
                        )
                        stdout = await proc.stdout.read.aio()
                        stderr = await proc.stderr.read.aio()
                        code = await proc.wait.aio()
                        artifacts = await self._collect_artifacts(sandbox)
                    except (ExecTimeoutError, SandboxTimeoutError):
                        # the job ran and blew past its own limit — a real
                        # result, never a fallback (the job already happened)
                        return JobResult(
                            exit_ok=False,
                            stdout="",
                            stderr="the job ran past its time limit and was stopped.",
                            artifacts={},
                            duration=time.monotonic() - started,
                            provider=self.name,
                            timed_out=True,
                        )
                    except ModalError as exc:
                        raise WorkbenchError(
                            f"the cloud sandbox failed mid-job — {type(exc).__name__}."
                        ) from exc
                    return JobResult(
                        exit_ok=code == 0,
                        stdout=clip(stdout, spec.limits.max_output_bytes),
                        stderr=clip(stderr, spec.limits.max_output_bytes),
                        artifacts=artifacts,
                        duration=time.monotonic() - started,
                        provider=self.name,
                    )
            except ModalError as exc:
                # the connection itself never opened — a dispatch failure,
                # same shape as a sandbox that didn't come up
                raise WorkbenchError(
                    f"the cloud sandbox didn't come up — {type(exc).__name__}."
                ) from exc
        finally:
            if sandbox is not None:
                # the guarantee: the sandbox dies whatever happened in it
                try:
                    await sandbox.terminate.aio()
                except Exception:
                    log.warning("a sandbox couldn't be cleaned up", exc_info=True)

    async def _collect_artifacts(self, sandbox: Any) -> dict[str, bytes]:
        """Whatever the job left in its artifacts directory, as bytes —
        the only files that ever come back. A job that wrote nothing
        back, or named a directory, simply returns fewer files."""
        out: dict[str, bytes] = {}
        try:
            entries = await sandbox.filesystem.list_files.aio(f"{_WORKDIR}/{_ARTIFACTS}")
        except Exception:
            return out  # no artifacts directory — nothing to collect
        files = [entry for entry in entries if entry.is_file()]
        reads = await asyncio.gather(
            *(
                sandbox.filesystem.read_bytes.aio(f"{_WORKDIR}/{_ARTIFACTS}/{entry.name}")
                for entry in files
            ),
            return_exceptions=True,
        )
        for entry, data in zip(files, reads, strict=True):
            if isinstance(data, BaseException):
                log.debug("an artifact couldn't be read back", exc_info=data)
                continue
            if len(data) > _ARTIFACT_LIMIT:
                log.info("an artifact was over the size limit and wasn't kept")
                continue
            out[entry.name] = data
        return out
