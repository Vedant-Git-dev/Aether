"""The workbench — where a job runs when it must not run here.

Routing is deterministic code, in this module, and the model is never in
it. The ladder never encodes provider names either: the caller hands an
ordered list (the configured default first — `ordered` does that), and
each provider answers for itself — `available` gates on its key and
quota, `accepts` is its own claim on what classes of work it may run
(a batch-only provider refuses sensitive and interactive jobs itself, so
the rule "sensitive or interactive never runs on the batch provider"
lives with the provider, not scattered through a router).

The ladder, once through:
- no provider up and accepting -> nobody's error to catch, the caller
  queues or refuses in plain words;
- a provider that takes the job but fails to dispatch (quota refused,
  create failed) -> fall through to the next provider that would have
  taken it, and if none remains, the last error surfaces;
- a job that ran and failed its own script's way (exit_ok=False) -> a
  real result, no fallback; the job already happened.
"""

from __future__ import annotations

from .base import (
    BACKGROUND,
    INTERACTIVE,
    LABEL,
    LATENCY_CLASS,
    ORDINARY,
    SENSITIVE,
    SENSITIVITY,
    TASK_ID,
    JobLimits,
    JobResult,
    JobSpec,
    Workbench,
    WorkbenchError,
    script_digest,
)
from .fake import FakeWorkbench

__all__ = [
    "BACKGROUND",
    "INTERACTIVE",
    "JobLimits",
    "JobResult",
    "JobSpec",
    "LABEL",
    "LATENCY_CLASS",
    "ORDINARY",
    "SENSITIVE",
    "SENSITIVITY",
    "TASK_ID",
    "Workbench",
    "WorkbenchError",
    "FakeWorkbench",
    "ordered",
    "route",
    "run",
    "script_digest",
]


def ordered(providers: list[Workbench], default: str) -> list[Workbench]:
    """The providers in routing order: the configured default first, the
    rest in the order given. The name is config vocabulary — the ladder
    itself only walks the list it is handed."""
    first = [wb for wb in providers if wb.name == default]
    return first + [wb for wb in providers if wb.name != default]


async def route(spec: JobSpec, providers: list[Workbench]) -> Workbench | None:
    """The first provider, in the order given, that is up and claims the
    job. No side effects — a route can be taken, inspected, and thrown
    away. None means nobody can take the job."""
    for wb in providers:
        if await wb.available() and await wb.accepts(spec):
            return wb
    return None


async def run(spec: JobSpec, providers: list[Workbench]) -> JobResult:
    """Route and run, falling through on dispatch failures. Raises
    WorkbenchError when no provider can take the job, or when every one
    that could failed to dispatch — the caller queues in both cases."""
    last: WorkbenchError | None = None
    for wb in providers:
        if not await wb.available():
            continue
        if not await wb.accepts(spec):
            continue
        try:
            return await wb.run_job(spec)
        except WorkbenchError as exc:
            last = exc
    if last is not None:
        raise last
    raise WorkbenchError(f"no workbench provider could take the job ({spec.label}) — nothing ran.")
