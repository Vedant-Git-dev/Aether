"""The workbench contract — one job in, one result out.

A job is a batch: a named script and the files it needs, nothing else.
The interface is deliberately narrow so every adapter (the fake used by
tests, the ephemeral-microVM providers, the batch runner in the user's
own code repository) is interchangeable, and so the guarantees hold
everywhere:

- the job sees ONLY the files in its spec — never Aether's keys, tokens,
  or the database URL;
- the provider never receives a shell command — a script file it runs
  its own way;
- run_job cleans up in finally, whatever happened inside the sandbox;
- the audit record carries the script by its digest, never its text —
  the full text lives encrypted in the task's own row.

Routing is deterministic code: the model never picks a provider. The
sensitivity and latency a spec carries decide which adapters may take
it, and an adapter that must not take it says so itself, through
`accepts` — fail-safe defaults (a missing sensitivity reads as
sensitive, a missing latency class as background) do the rest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

# the meta keys a spec carries — the vocabulary `accepts` reads
SENSITIVITY = "sensitivity"
LATENCY_CLASS = "latency_class"
TASK_ID = "task_id"
LABEL = "label"

# the values the ladder understands. "sensitive" is the fail-safe: an
# unlabeled spec may only run where a sensitive one can.
SENSITIVE = "sensitive"
ORDINARY = "ordinary"
INTERACTIVE = "interactive"
BACKGROUND = "background"


class WorkbenchError(Exception):
    """A job could not be dispatched or completed — a quota refusal, a
    dispatch failure, a timeout at the provider. The caller may fall back
    to another provider or queue the task. Anything else (a bug) is not
    a WorkbenchError and propagates."""


@dataclass(frozen=True)
class JobLimits:
    timeout_seconds: int = 600
    max_output_bytes: int = 1_000_000


@dataclass(frozen=True)
class JobSpec:
    """One job — a named script plus the files it needs. The dict of
    files is the job's whole world; the meta carries who asked for it
    (task_id, label) and what class of work it is (sensitivity,
    latency_class)."""

    script_name: str
    files: dict[str, str] = field(default_factory=dict)
    limits: JobLimits = field(default_factory=JobLimits)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def sensitivity(self) -> str:
        # unlabeled fails safe: only a provider that takes sensitive
        # work may run it
        value = self.meta.get(SENSITIVITY)
        return value if value in (SENSITIVE, ORDINARY) else SENSITIVE

    @property
    def latency_class(self) -> str:
        value = self.meta.get(LATENCY_CLASS)
        return value if value in (INTERACTIVE, BACKGROUND) else BACKGROUND

    @property
    def script_text(self) -> str:
        return self.files.get(self.script_name, "")

    @property
    def label(self) -> str:
        return str(self.meta.get(LABEL) or self.script_name)


@dataclass(frozen=True)
class JobResult:
    """What came back — the exit shape, the streams, any files the job
    produced, how long it ran, and which provider ran it. A result with
    exit_ok=False is a completed job that failed its own script's way;
    dispatch failures are exceptions, never results."""

    exit_ok: bool
    stdout: str
    stderr: str
    artifacts: dict[str, bytes] = field(default_factory=dict)
    duration: float = 0.0
    provider: str = ""
    timed_out: bool = False


class Workbench(Protocol):
    """The adapter contract. `available` gates on the adapter's key and
    quota; `accepts` is the adapter's own claim on what classes of work
    it may run (a batch-only provider refuses sensitive and interactive
    jobs here, itself — the ladder never encodes provider names);
    `run_job` does the work and cleans up in finally."""

    name: str

    async def available(self) -> bool: ...

    async def accepts(self, spec: JobSpec) -> bool: ...

    async def run_job(self, spec: JobSpec) -> JobResult: ...


class KeyResolver(Protocol):
    """The resolver surface every adapter needs — a secret's name in,
    its value out, fresh each call, so a key pasted after boot works
    with no restart."""

    async def resolve(self, name: str) -> str | None: ...


def script_digest(spec: JobSpec) -> str:
    """The script's content digest — the only form the audit record ever
    carries. The full text stays encrypted in the task's own row."""
    return hashlib.sha256(spec.script_text.encode("utf-8")).hexdigest()
