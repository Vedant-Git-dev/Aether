"""The skill contract — the background worker's plug-in shape.

A skill is deterministic code the worker runs in phases: it DETECTS
opportunities from memory and the profile, PLANS one task in a few
plain fields, RUNS it with the tools the policy allows, and
REVALIDATES before anything the user approved actually fires. The model
is never in this loop — detection and planning are code, and the only
judgment a skill makes is the honest kind: a score and a plain-words
label.

The phases gate tools in code, not in prompts. detect and plan receive
an executor that only accepts reads — the worker wraps it before
handing it over (see `proactive.worker.read_only`) — while run receives
the full executor, where the policy still parks anything outbound for
approval. A skill that tries to make a change while planning is refused
in plain words before the policy is even asked.

These three skills are examples of the shape, deliberately small:
they demonstrate each seam (a parked send, a web lookup kept as a note,
a sandbox job) without pretending to more judgment than code has.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..llm.types import ToolCall
from ..proactive.store import Opportunity, ProactiveTask
from ..proactive.worker import RunOutcome
from ..workbench.base import JobResult, JobSpec

# one tool call in, the tool's content out — parked or not
Executor = Callable[[ToolCall], Awaitable[str]]
# one job spec in, one result out — the routing ladder already walked
Runner = Callable[[JobSpec], Awaitable[JobResult]]

# how a skill reads the replies the loop's executor gives it. The
# wording lives in agent/loop.py; the skills match the prefixes so a
# staged action can be told from a settled one without parsing anything
HELD = "held for approval"  # the call parked — the user has been asked
DENIED = "denied by policy"  # the call was refused outright

# the parked call's id inside its reply — "held for approval (#9) — …".
# Reading it back is the only way a skill can tell the worker WHICH
# staged action its task is waiting on, so the task's own Approve button
# can carry exactly that one out.
_HELD_ID = re.compile(rf"{re.escape(HELD)} \(#(\d+)\)")


def held_approval(reply: str) -> int | None:
    """The approval id from a parked call's reply, or None when the reply
    isn't a parked one. The number is the loop's own — the same one the
    one-tap buttons answer."""
    found = _HELD_ID.search(reply)
    return int(found.group(1)) if found else None


class ProfileSource(Protocol):
    """The workspace slice the skills read — the confirmed profile."""

    def profile(self) -> list[Any]: ...


class EventReader(Protocol):
    """The memory slice the skills read — memorable history, and one
    event by id for revalidation."""

    async def recent_memorable(self, limit: int = 50, hours: float | None = None) -> list[Any]: ...

    async def get(self, event_id: int) -> Any | None: ...


class Skill(Protocol):
    """Everything the worker orchestrates and the panel explains, in one
    shape. `declared_actions` and `description` are plain words for the
    cards shown before anything runs; `sensitivity` is the workbench
    vocabulary (SENSITIVE when the skill's work leaves Aether or the
    user's own surfaces), so a job a skill dispatches can inherit an
    honest class instead of failing safe."""

    name: str
    description: str
    declared_actions: tuple[str, ...]
    sensitivity: str

    async def detect(
        self, profile: ProfileSource | None, events: EventReader | None, executor: Executor | None
    ) -> list[Opportunity]: ...

    async def plan(
        self,
        opportunity: Opportunity,
        profile: ProfileSource | None,
        executor: Executor | None,
    ) -> dict[str, Any]: ...

    async def run(
        self, task: ProactiveTask, workbench: Runner | None, executor: Executor | None
    ) -> RunOutcome: ...

    async def revalidate(
        self, opportunity: Opportunity, profile: ProfileSource | None, events: EventReader | None
    ) -> bool: ...
