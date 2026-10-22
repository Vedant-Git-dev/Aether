"""The batch workbench adapter — a queued job in the user's own code
repository, for work that can wait and may be watched.

The trust class is narrower than a dedicated sandbox, and the adapter
says so itself through `accepts`: a repository's queued runs are visible
to its readers and take minutes to start, so it takes only ordinary,
patient work — anything sensitive, interactive, or unlabeled (which
reads as sensitive) is refused here and the ladder carries it to a
dedicated sandbox instead.

What crosses into the repository: two unguessable one-time URLs — one
GET for the job's files, one POST for its outcome — and nothing else.
No Aether secret rides along; each URL dies on its first use and both
are dead within half an hour of the job's own clock. Because both URLs
are single-use, even a duplicated dispatch cannot run a job twice: the
second runner finds its bundle already spent.

The two failure shapes, kept straight: a dispatch the queue never took
is a WorkbenchError the ladder can fall through on — nothing ran, so
one retry is safe. Once the queue has accepted the job, the outcome is
always a real result (the posted exit code, or a timeout if it never
came back), never a fallback — the job already happened. Runs are
deleted after collect so the repository doesn't accumulate history, and
every job is metered against a monthly minutes cap that fails closed: a
meter that can't be read makes the adapter simply unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from random import uniform
from typing import Any, Protocol

import httpx

from .base import (
    BACKGROUND,
    ORDINARY,
    SAFE_NAME,
    TASK_ID,
    JobResult,
    JobSpec,
    KeyResolver,
    WorkbenchError,
    clip,
    script_digest,
    validate_names,
)
from .minutes import MeterState
from .transfers import BUNDLE, RESULT, TransferTicket

log = logging.getLogger("aether.workbench.github_actions")

_TOKEN = "GITHUB_ACTIONS_TOKEN"
# the transfer URLs outlive the job's own clock by the slack, so a slow
# runner still finds them live right after being stopped at the limit
_TTL_SLACK = 1800
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Refused(Exception):
    """The queue answered no — retrying won't change it."""


class _Transient(Exception):
    """The dispatch didn't land — nothing ran, so it may be tried once more."""


class TransfersApi(Protocol):
    """The transfer-store seam — mint a job's two one-time URLs, collect
    the outcome — so no test ever touches Postgres."""

    async def mint(self, *, kind: str, payload: dict, ttl_seconds: int) -> TransferTicket: ...

    async def collect_result(self, token: str) -> dict[str, Any] | None: ...


class MinutesApi(Protocol):
    """The meter seam — this month against the cap, and a job's charge."""

    async def check(self, cap_minutes: int, *, now: datetime | None = None) -> MeterState: ...

    async def charge(self, seconds: float, *, now: datetime | None = None) -> int: ...


class GitHubActionsWorkbench:
    """The batch provider. Inert without its key, its repository, and a
    readable minute meter; takes only ordinary background work."""

    def __init__(
        self,
        *,
        resolver: KeyResolver,
        transfers: TransfersApi,
        public_url: str,
        repo: str,
        workflow: str,
        ref: str = "main",
        api_url: str = "https://api.github.com",
        monthly_minutes_cap: int = 1500,
        minutes: MinutesApi | None = None,
        poll_seconds: float = 5.0,
        jitter_seconds: float = 2.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._resolver = resolver
        self._transfers = transfers
        self._public_url = public_url.rstrip("/")
        self._repo = repo
        self._workflow = workflow
        self._ref = ref
        self._api_url = api_url.rstrip("/")
        self._cap = monthly_minutes_cap
        self._minutes = minutes
        self._poll_seconds = poll_seconds
        self._jitter_seconds = jitter_seconds
        self._owns_client = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._now = now
        self._sleep = sleep

    @property
    def name(self) -> str:
        return "github_actions"

    async def aclose(self) -> None:
        """Close the HTTP client if this adapter built its own."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubActionsWorkbench:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def available(self) -> bool:
        if not await self._resolver.resolve(_TOKEN):
            return False
        if self._minutes is None:
            return False
        if not (
            self._public_url and _REPO.fullmatch(self._repo) and SAFE_NAME.fullmatch(self._workflow)
        ):
            return False
        try:
            state = await self._minutes.check(self._cap)
        except Exception:  # noqa: BLE001 — fail closed: an unreadable meter means no batch
            log.warning(
                "the batch minute meter couldn't be read — the batch queue stays off", exc_info=True
            )
            return False
        return state.allowed

    async def accepts(self, spec: JobSpec) -> bool:
        # a repository's queued runs are visible to its readers and take
        # minutes to start — ordinary, patient work only. Everything
        # else is refused here, by the adapter itself, so the ladder
        # never encodes a provider's name.
        return spec.latency_class == BACKGROUND and spec.sensitivity == ORDINARY

    async def run_job(self, spec: JobSpec) -> JobResult:
        token = await self._resolver.resolve(_TOKEN)
        if not token:
            raise WorkbenchError(
                "the batch queue key isn't set — it can be pasted on the settings page."
            )
        if not (
            self._public_url and _REPO.fullmatch(self._repo) and SAFE_NAME.fullmatch(self._workflow)
        ):
            raise WorkbenchError(
                "the batch queue isn't configured — a public address, a repository, "
                "and a workflow file are needed."
            )
        if self._minutes is None:
            raise WorkbenchError(
                "the batch queue has no minute meter — its jobs aren't run unmetered."
            )
        validate_names(spec)
        started = self._clock()
        try:
            state = await self._minutes.check(self._cap)
        except Exception as exc:
            raise WorkbenchError(
                "the batch queue's minutes couldn't be read — the job wasn't sent."
            ) from exc
        if not state.allowed:
            raise WorkbenchError(
                "the batch queue is out of minutes for this month — the job wasn't sent."
            )
        if state.near_limit:
            log.info("the batch queue is past 80% of its monthly minutes")

        ttl = spec.limits.timeout_seconds + _TTL_SLACK
        bundle = await self._transfers.mint(
            kind=BUNDLE,
            payload={"script_name": spec.script_name, "files": spec.files},
            ttl_seconds=ttl,
        )
        result_ticket = await self._transfers.mint(kind=RESULT, payload={}, ttl_seconds=ttl)
        inputs = {
            "session": str(spec.meta.get(TASK_ID) or script_digest(spec)[:16]),
            "bundle": f"{self._public_url}/api/workbench/{BUNDLE}/{bundle.token}",
            "result": f"{self._public_url}/api/workbench/{RESULT}/{result_ticket.token}",
        }
        await self._dispatch_with_retry(token, inputs)
        dispatched_at = self._now()
        deadline = started + spec.limits.timeout_seconds

        run_id: int | None = None
        completed = False
        while True:
            run = await self._find_run(token, dispatched_at)
            if run is not None:
                if run.get("id") is not None:
                    run_id = int(run["id"])
                if run.get("status") == "completed":
                    completed = True
                    break
            if self._clock() >= deadline:
                break
            await self._sleep(self._poll_seconds + uniform(0, self._jitter_seconds))
        duration = self._clock() - started

        if not completed:
            # the queue accepted the job, so this is a real result whatever
            # happened to the run — never a fallback, never a re-run
            if run_id is not None:
                await self._best_effort_cancel(token, run_id)
                await self._best_effort_delete(token, run_id)
            await self._charge_best_effort(duration)
            return JobResult(
                exit_ok=False,
                stdout="",
                stderr="the job ran past its time limit and was stopped.",
                artifacts={},
                duration=duration,
                provider=self.name,
                timed_out=True,
            )
        posted = await self._transfers.collect_result(result_ticket.token)
        if run_id is not None:
            await self._best_effort_delete(token, run_id)
        await self._charge_best_effort(duration)
        if posted is None:
            return JobResult(
                exit_ok=False,
                stdout="",
                stderr="the batch job finished without reporting back.",
                artifacts={},
                duration=duration,
                provider=self.name,
            )
        try:
            exit_code = int(posted.get("exit_code", 1))
        except (TypeError, ValueError):
            exit_code = 1
        return JobResult(
            exit_ok=exit_code == 0,
            stdout=clip(str(posted.get("stdout") or ""), spec.limits.max_output_bytes),
            stderr=clip(str(posted.get("stderr") or ""), spec.limits.max_output_bytes),
            artifacts={},  # batch jobs carry no files back — the report is the result
            duration=duration,
            provider=self.name,
        )

    # -- the queue, over http ------------------------------------------------------

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _dispatch(self, token: str, inputs: dict[str, str]) -> None:
        """One dispatch attempt. _Refused means the queue answered no;
        _Transient means the attempt didn't land."""
        url = f"{self._api_url}/repos/{self._repo}/actions/workflows/{self._workflow}/dispatches"
        try:
            resp = await self._client.post(
                url, json={"ref": self._ref, "inputs": inputs}, headers=self._headers(token)
            )
        except httpx.HTTPError as exc:
            raise _Transient() from exc
        if resp.status_code >= 500:
            raise _Transient()
        if resp.status_code >= 400:
            raise _Refused(
                f"the batch queue refused the job (HTTP {resp.status_code}) — its monthly "
                "minutes may be spent, or the workflow can't be dispatched."
            )

    async def _dispatch_with_retry(self, token: str, inputs: dict[str, str]) -> None:
        # nothing has run yet, so one retry is safe — and even a duplicated
        # dispatch can't double-run the job: the bundle URL dies on first use
        try:
            await self._dispatch(token, inputs)
            return
        except _Refused as exc:
            raise WorkbenchError(str(exc)) from exc
        except _Transient:
            pass  # nothing landed — one more try
        try:
            await self._dispatch(token, inputs)
        except _Refused as exc:
            raise WorkbenchError(str(exc)) from exc
        except _Transient as exc:
            raise WorkbenchError("the batch queue didn't take the job — nothing ran.") from exc

    async def _find_run(self, token: str, since: datetime) -> dict[str, Any] | None:
        """The newest run of this workflow created at or just after our
        dispatch. The two-second slack covers clock skew between the
        queue's timestamps and ours; a poll hiccup just reads as 'not yet'."""
        url = f"{self._api_url}/repos/{self._repo}/actions/workflows/{self._workflow}/runs?per_page=10"
        try:
            resp = await self._client.get(url, headers=self._headers(token))
            if resp.status_code != 200:
                return None
            runs = resp.json().get("workflow_runs") or []
        except (httpx.HTTPError, ValueError):
            return None
        earliest = since - timedelta(seconds=2)
        best: dict[str, Any] | None = None
        best_at: datetime | None = None
        for run in runs:
            created = _parse_iso(str(run.get("created_at") or ""))
            if created is None or created < earliest:
                continue
            if best_at is None or created > best_at:
                best, best_at = run, created
        return best

    async def _best_effort_cancel(self, token: str, run_id: int) -> None:
        try:
            await self._client.post(
                f"{self._api_url}/repos/{self._repo}/actions/runs/{run_id}/cancel",
                headers=self._headers(token),
            )
        except httpx.HTTPError:
            log.debug("the batch run couldn't be cancelled", exc_info=True)

    async def _best_effort_delete(self, token: str, run_id: int) -> None:
        # the repository shouldn't accumulate the job's history
        try:
            await self._client.delete(
                f"{self._api_url}/repos/{self._repo}/actions/runs/{run_id}",
                headers=self._headers(token),
            )
        except httpx.HTTPError:
            log.debug("the batch run couldn't be deleted", exc_info=True)

    async def _charge_best_effort(self, seconds: float) -> None:
        # the outcome is real and must not be lost to a meter hiccup — an
        # undercount only weakens the cap, and available() reads fail closed
        if self._minutes is None:
            return
        try:
            await self._minutes.charge(seconds)
        except Exception:  # noqa: BLE001 — never lose a real result over bookkeeping
            log.warning("the batch minute meter couldn't record the job's time", exc_info=True)
