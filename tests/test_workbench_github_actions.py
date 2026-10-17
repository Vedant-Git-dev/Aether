"""The batch workbench adapter, against a scripted queue — no repository
is ever touched, no network is ever opened.

What's pinned here: inert without the key, the repository, or a readable
meter; only ordinary, patient work is claimed (the adapter refuses the
rest itself, so the ladder never encodes its name); the dispatch carries
nothing but a session id and two one-time URLs; a dispatch that didn't
land is tried exactly once more and a refusal not at all — but once the
queue has accepted the job the outcome is always a real result: the
posted exit code, a timeout if it never came back, never a fallback.
Every finished job is metered, and its run is deleted from the
repository so history doesn't accumulate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from aether.workbench import JobLimits, JobSpec, WorkbenchError
from aether.workbench.github_actions import GitHubActionsWorkbench
from aether.workbench.minutes import MeterState
from aether.workbench.transfers import BUNDLE, TransferTicket

# -- fakes ------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, token: str | None) -> None:
        self.token = token

    async def resolve(self, name: str) -> str | None:
        return self.token if name == "GITHUB_ACTIONS_TOKEN" else None


@dataclass
class Minted:
    kind: str
    token: str
    payload: dict
    ttl_seconds: int


class FakeTransfers:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result_payload = result
        self.minted: list[Minted] = []
        self.collected: list[str] = []

    async def mint(self, *, kind: str, payload: dict, ttl_seconds: int) -> TransferTicket:
        token = f"tok{len(self.minted)}"
        self.minted.append(Minted(kind, token, payload, ttl_seconds))
        return TransferTicket(
            kind=kind, token=token, expires_at=datetime(2026, 10, 17, 12, 30, tzinfo=UTC)
        )

    async def collect_result(self, token: str) -> dict[str, Any] | None:
        self.collected.append(token)
        return self.result_payload


class FakeMeter:
    def __init__(
        self,
        state: MeterState | None = None,
        check_raises: Exception | None = None,
        charge_raises: Exception | None = None,
    ) -> None:
        self.state = state
        self.check_raises = check_raises
        self.charge_raises = charge_raises
        self.charged: list[float] = []
        self.caps: list[int] = []

    async def check(self, cap_minutes: int, *, now: datetime | None = None) -> MeterState:
        if self.check_raises is not None:
            raise self.check_raises
        self.caps.append(cap_minutes)
        return self.state or MeterState(0, cap_minutes, True, False)

    async def charge(self, seconds: float, *, now: datetime | None = None) -> int:
        if self.charge_raises is not None:
            raise self.charge_raises
        self.charged.append(seconds)
        return 0


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.t += seconds


class FakeQueue:
    """The repository's REST surface, scripted: dispatch answers, then a
    list of statuses the polls hand out in order (the last one repeats)."""

    def __init__(
        self,
        statuses: list[str] | None = None,
        dispatch_responses: list[httpx.Response] | None = None,
        no_runs: bool = False,
        drop_first_dispatch: bool = False,
    ) -> None:
        self.statuses = statuses or []
        self.dispatch_responses = dispatch_responses or [httpx.Response(204)]
        self.no_runs = no_runs
        self.drop_first_dispatch = drop_first_dispatch
        self.calls: list[tuple[str, str]] = []
        self.dispatched_inputs: list[dict[str, str]] = []
        self._runs_seen = 0

    def _runs(self) -> list[dict[str, Any]]:
        if self.no_runs:
            return []
        status = (
            self.statuses[self._runs_seen]
            if self._runs_seen < len(self.statuses)
            else (self.statuses[-1] if self.statuses else "in_progress")
        )
        self._runs_seen += 1
        return [
            {
                "id": 777,
                "status": status,
                "created_at": "2026-10-17T12:00:05Z",  # just after our dispatch
            }
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if url.endswith("/dispatches"):
            if self.drop_first_dispatch:
                self.drop_first_dispatch = False
                raise httpx.ConnectError("nothing landed")
            self.dispatched_inputs.append(json.loads(request.read())["inputs"])
            return self.dispatch_responses.pop(0)
        if request.method == "POST" and url.endswith("/cancel"):
            return httpx.Response(204)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET" and "/runs" in url:
            return httpx.Response(200, json={"workflow_runs": self._runs()})
        return httpx.Response(404)


def spec(**meta: Any) -> JobSpec:
    return JobSpec(script_name="job.py", files={"job.py": "print('hi')"}, meta=meta)


def urgent_spec() -> JobSpec:
    return JobSpec(
        script_name="job.py",
        files={"job.py": "print('hi')"},
        limits=JobLimits(timeout_seconds=12),
        meta={"sensitivity": "ordinary", "latency_class": "background", "task_id": 42},
    )


@dataclass
class Rig:
    wb: GitHubActionsWorkbench
    queue: FakeQueue
    transfers: FakeTransfers
    meter: FakeMeter
    sleep: FakeSleep


def rig(
    queue: FakeQueue | None = None,
    transfers: FakeTransfers | None = None,
    meter: FakeMeter | None = None,
) -> Rig:
    queue = queue or FakeQueue(statuses=["completed"])
    transfers = transfers or FakeTransfers(result={"exit_code": 0, "stdout": "done", "stderr": ""})
    meter = meter or FakeMeter()
    clock = FakeClock()
    sleep = FakeSleep(clock)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(queue.handler),
        base_url="https://api.github.test",  # never opened — MockTransport answers locally
    )
    wb = GitHubActionsWorkbench(
        resolver=FakeResolver("repo-key"),
        transfers=transfers,
        public_url="https://aether.example",
        repo="you/your-repo",
        workflow="aether-batch.yml",
        api_url="https://api.github.test",
        minutes=meter,
        client=client,
        clock=clock,
        now=lambda: datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        sleep=sleep,
    )
    return Rig(wb=wb, queue=queue, transfers=transfers, meter=meter, sleep=sleep)


def blank_workbench() -> GitHubActionsWorkbench:
    return GitHubActionsWorkbench(
        resolver=FakeResolver("repo-key"),
        transfers=FakeTransfers(),
        public_url="",
        repo="",
        workflow="",
        minutes=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(FakeQueue().handler)),
    )


# -- the gate ---------------------------------------------------------------------


async def test_inert_without_the_key() -> None:
    r = rig()
    r.wb._resolver = FakeResolver(None)  # type: ignore[attr-defined] — swap the key out
    assert not await r.wb.available()
    with pytest.raises(WorkbenchError, match="key isn't set"):
        await r.wb.run_job(spec())
    assert r.queue.calls == []


async def test_inert_without_a_repository_or_a_meter() -> None:
    assert not await blank_workbench().available()


async def test_a_meter_outage_keeps_the_queue_off() -> None:
    r = rig(meter=FakeMeter(check_raises=RuntimeError("db gone")))
    assert not await r.wb.available()
    with pytest.raises(WorkbenchError, match="couldn't be read"):
        await r.wb.run_job(urgent_spec())
    assert r.queue.calls == []
    assert r.transfers.minted == []


async def test_a_meter_past_the_cap_refuses_the_job() -> None:
    r = rig(meter=FakeMeter(state=MeterState(1500, 1500, allowed=False, near_limit=True)))
    assert not await r.wb.available()
    with pytest.raises(WorkbenchError, match="out of minutes"):
        await r.wb.run_job(urgent_spec())
    assert r.transfers.minted == []
    assert r.queue.calls == []


async def test_only_ordinary_patient_work_is_claimed() -> None:
    r = rig()
    assert await r.wb.accepts(spec(sensitivity="ordinary", latency_class="background"))
    # sensitive work, interactive work, and unlabeled work (which reads
    # as sensitive) all belong on a dedicated sandbox, not here
    assert not await r.wb.accepts(spec(sensitivity="sensitive", latency_class="background"))
    assert not await r.wb.accepts(spec(sensitivity="ordinary", latency_class="interactive"))
    assert not await r.wb.accepts(spec())


# -- the dispatch -----------------------------------------------------------------


async def test_the_dispatch_carries_only_the_session_and_two_one_time_urls() -> None:
    r = rig()
    result = await r.wb.run_job(urgent_spec())
    assert result.exit_ok
    assert result.provider == "github_actions"
    assert result.artifacts == {}
    assert result.stdout == "done"
    bundle, result_ticket = r.transfers.minted
    assert bundle.kind == BUNDLE
    assert bundle.payload == {"script_name": "job.py", "files": {"job.py": "print('hi')"}}
    assert r.queue.dispatched_inputs == [
        {
            "session": "42",  # the task's own id
            "bundle": f"https://aether.example/api/workbench/bundle/{bundle.token}",
            "result": f"https://aether.example/api/workbench/result/{result_ticket.token}",
        }
    ]
    assert r.meter.charged  # the job's time was metered
    assert r.meter.caps == [1500]


async def test_a_dispatch_that_never_lands_is_tried_once_more() -> None:
    r = rig(queue=FakeQueue(statuses=["completed"], drop_first_dispatch=True))
    result = await r.wb.run_job(urgent_spec())
    assert result.exit_ok
    posts = [c for c in r.queue.calls if c[0] == "POST" and c[1].endswith("/dispatches")]
    assert len(posts) == 2  # tried once more — nothing had run


async def test_two_failed_dispatches_mean_nothing_ran() -> None:
    queue = FakeQueue(statuses=[], dispatch_responses=[httpx.Response(503), httpx.Response(503)])
    r = rig(queue=queue)
    with pytest.raises(WorkbenchError, match="didn't take the job"):
        await r.wb.run_job(urgent_spec())
    posts = [c for c in r.queue.calls if c[0] == "POST"]
    assert len(posts) == 2  # exactly one retry
    assert r.meter.charged == []
    assert r.transfers.collected == []


async def test_a_refusal_is_not_retried() -> None:
    queue = FakeQueue(statuses=[], dispatch_responses=[httpx.Response(403)])
    r = rig(queue=queue)
    with pytest.raises(WorkbenchError, match="refused the job"):
        await r.wb.run_job(urgent_spec())
    posts = [c for c in r.queue.calls if c[0] == "POST"]
    assert len(posts) == 1


# -- the outcome ------------------------------------------------------------------


async def test_a_failed_job_reports_its_own_exit_code() -> None:
    r = rig(transfers=FakeTransfers(result={"exit_code": 3, "stdout": "partial", "stderr": "boom"}))
    result = await r.wb.run_job(urgent_spec())
    assert not result.exit_ok
    assert not result.timed_out
    assert result.stderr == "boom"
    assert r.meter.charged


async def test_a_run_that_finished_without_reporting_back_is_a_real_failure() -> None:
    r = rig(transfers=FakeTransfers(result=None))
    result = await r.wb.run_job(urgent_spec())
    assert not result.exit_ok
    assert not result.timed_out
    assert "without reporting back" in result.stderr
    assert r.meter.charged


async def test_a_timeout_cancels_the_run_and_is_a_real_result() -> None:
    r = rig(queue=FakeQueue(statuses=["in_progress", "in_progress"]))
    result = await r.wb.run_job(urgent_spec())
    assert not result.exit_ok
    assert result.timed_out
    assert "time limit" in result.stderr
    cancels = [c for c in r.queue.calls if c[1].endswith("/cancel")]
    deletes = [c for c in r.queue.calls if c[0] == "DELETE"]
    assert len(cancels) == 1
    assert len(deletes) == 1  # the history doesn't linger either
    assert r.meter.charged


async def test_a_run_that_never_appeared_is_a_real_result_too() -> None:
    # the queue accepted the job, so there is no falling back no matter
    # what happened to the run — nothing is ever run twice
    r = rig(queue=FakeQueue(no_runs=True))
    result = await r.wb.run_job(urgent_spec())
    assert not result.exit_ok
    assert result.timed_out
    cancels = [c for c in r.queue.calls if c[1].endswith("/cancel")]
    assert cancels == []  # nothing to cancel — no run ever showed up
    assert r.meter.charged


async def test_huge_output_is_clipped_to_the_jobs_own_limit() -> None:
    r = rig(transfers=FakeTransfers(result={"exit_code": 0, "stdout": "y" * 5000, "stderr": ""}))
    s = JobSpec(
        script_name="job.py",
        files={"job.py": "x"},
        limits=JobLimits(timeout_seconds=600, max_output_bytes=100),
    )
    result = await r.wb.run_job(s)
    assert len(result.stdout) == 101  # 100 + the ellipsis


async def test_a_finished_run_is_deleted_from_the_repository() -> None:
    r = rig()
    await r.wb.run_job(urgent_spec())
    deletes = [c for c in r.queue.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][1].endswith("/runs/777")


async def test_a_bad_name_is_refused_before_anything_is_minted() -> None:
    r = rig()
    s = JobSpec(script_name="../job.py", files={"../job.py": "print('hi')"})
    with pytest.raises(WorkbenchError):
        await r.wb.run_job(s)
    assert r.queue.calls == []
    assert r.transfers.minted == []


async def test_a_meter_hiccup_never_loses_a_real_result() -> None:
    r = rig(meter=FakeMeter(charge_raises=RuntimeError("db blip")))
    result = await r.wb.run_job(urgent_spec())
    assert result.exit_ok
    assert r.meter.charged == []  # the charge was attempted and lost — the result stands


async def test_the_session_falls_back_to_the_script_digest() -> None:
    r = rig()
    await r.wb.run_job(spec(sensitivity="ordinary", latency_class="background"))
    session = r.queue.dispatched_inputs[0]["session"]
    assert len(session) == 16  # the digest's first 16 hex characters
