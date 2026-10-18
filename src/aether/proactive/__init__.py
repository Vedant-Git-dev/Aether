"""The proactive worker — Aether's background loop for the "works while
you sleep" mode. The pieces, and the order they compose in:

- ``store`` — task rows in Postgres, one per opportunity ever taken. The
  unique (skill, opportunity key) index is the dedup: the same thread or
  file can never re-detect into a second task, so a discarded opportunity
  stays discarded and budget can't be burned twice on it.
- ``budget`` — the daily caps (tasks, tokens, sandbox minutes) against the
  shared daily counters. Exhausted means the cycle skips; it never crashes.
- ``worker`` — the SchedulerWorker-shaped loop: kill-switch, boot re-arm,
  detect, score, select, then per task: claim, plan, run, store, notify.

Skills (what to detect and how to act) plug in from ``aether.skills``;
the worker only orchestrates. Routing is deterministic code throughout —
the model never picks providers, and every tool a skill calls still goes
through the policy gate.
"""

from .budget import Budget
from .store import (
    APPROVED,
    DISCARDED,
    DONE,
    FAILED,
    PENDING,
    REVISED,
    RUNNING,
    Opportunity,
    ProactiveStore,
    ProactiveTask,
)
from .worker import ProactiveWorker, RunOutcome

__all__ = [
    "APPROVED",
    "Budget",
    "DISCARDED",
    "DONE",
    "FAILED",
    "Opportunity",
    "PENDING",
    "ProactiveStore",
    "ProactiveTask",
    "ProactiveWorker",
    "REVISED",
    "RunOutcome",
    "RUNNING",
]
