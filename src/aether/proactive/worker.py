"""The proactive worker — the background loop that looks for work while
the user is away, in the SchedulerWorker shape (start/stop asyncio task,
boot re-arm, one audited transition per step) so the guarantees are the
same ones the scheduler already keeps: everything survives a restart via
rows, and nothing runs twice.

One cycle, in order:

1. kill-switch — ``proactive.enabled`` is read live; off means nothing
   happens (the loop still spins, so /resume needs no restart);
2. crash recovery — pending rows (re-armed from ``running`` at boot)
   run before anything new is detected, because their keys are already
   taken and detection can never re-claim them;
3. budget — ``room`` is zero when any daily cap is spent: skip, never
   crash, never spend;
4. detect — every enabled skill is asked what it found;
5. score and select — deterministic: the skill's own estimate times the
   feedback weight derived from the last 30 days, highest first, at most
   the budget's room;
6. per task: claim (the unique index refuses a duplicate opportunity),
   plan, run, store the result, charge the budget.

The anomaly monitor watches how runs come back: three refused runs from
one skill, or three discards in a row, flips ``proactive.enabled`` off
through the config store — a pause that survives restarts — and sends one
plain notification. The model is nowhere in any of this: skills decide
what's worth doing, deterministic code decides what runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..authz.approvals import DENIED
from ..config import AppConfig
from ..connectors.base import TASK_VERDICTS
from ..counters import today
from ..llm.types import ToolCall
from .budget import Budget
from .store import Opportunity, ProactiveStore, ProactiveTask

log = logging.getLogger("aether.proactive")

# how many bad outcomes in a row before the worker pauses itself
_AUTO_PAUSE_STREAK = 3

_PAUSE_NOTE = (
    "I paused the background worker — too many tasks came back wrong. "
    "Say /resume to start it again."
)

Notifier = Callable[[str], Awaitable[None]]
Pauser = Callable[[], Awaitable[None]]
Executor = Callable[[ToolCall], Awaitable[str]]
# (task id, plain-words result, which one-tap verdicts apply) — the
# result notification, routed through the loop so it rides the same
# interruption budget as every other unprompted word
Presenter = Callable[[int, str, tuple[str, ...]], Awaitable[None]]
# decide a parked approval and carry the fresh verdict out — the loop's own
# path, so a carried-out action gets its trace, its audit, and its words
ApprovalCarrier = Callable[[int, str], Awaitable[Any]]

# the read-only vocabulary the planning phases (detect, plan) may use —
# memory and workspace reads plus the web research pair. Everything
# else is a change, and changes wait for the run phase; this gate is
# code, so a policy ALLOW during planning still cannot carry a write.
READ_TOOLS = frozenset(
    {"memory_search", "workspace_read", "workspace_search", "web_fetch", "web_search"}
)


def read_only(executor: Executor) -> Executor:
    """The planning-phase executor: reads pass through untouched, a write
    is refused in plain words before the policy is even asked — the phase
    gate, deterministic, in the orchestrator rather than the prompt."""

    async def guarded(call: ToolCall) -> str:
        if call.name not in READ_TOOLS:
            return (
                "not run — planning only looks things up, it never makes "
                "changes; the call has to wait for the run phase"
            )
        return await executor(call)

    return guarded


@dataclass
class RunOutcome:
    """What one run came back with. ``refused`` marks a run the policy
    gate stopped outright — a denied action — which the anomaly monitor
    counts separately from a failure, because it means the skill keeps
    proposing things it may not do. ``failed`` marks a run that happened
    and produced no result — a job that failed its own way — so the row
    fails honestly while still charging what the run truly spent.
    ``approval_id`` names the parked action a run staged, when it did —
    the one the task's Approve button carries out after revalidation."""

    summary: str
    provider: str = ""
    tokens: int = 0
    sandbox_seconds: int = 0
    refused: bool = False
    failed: bool = False
    approval_id: int | None = None


class SkillSource(Protocol):
    """The slice of a skill the worker orchestrates — the full contract
    (sensitivity, declared actions, revalidation, plain-words summary)
    lives with the skills themselves; this is the worker's read of it.
    The executor detect and plan receive is read-gated (see ``read_only``);
    run receives the full one, where the policy still parks anything
    outbound."""

    name: str

    async def detect(self, profile: Any, events: Any, executor: Executor | None) -> list[Any]: ...

    async def plan(
        self, opportunity: Any, profile: Any, executor: Executor | None
    ) -> dict[str, Any]: ...

    async def run(
        self, task: ProactiveTask, workbench: Any, executor: Executor | None
    ) -> RunOutcome: ...

    async def revalidate(self, opportunity: Any, profile: Any, events: Any) -> bool: ...


class ProactiveWorker:
    """Background loop: re-arm on boot, then one cycle per interval."""

    def __init__(
        self,
        *,
        store: ProactiveStore,
        budget: Budget,
        config: AppConfig,
        skills: Sequence[SkillSource] = (),
        events: Any = None,
        workspace: Any = None,
        workbench: Any = None,
        executor: Any = None,
        notify: Notifier | None = None,
        pause: Pauser | None = None,
        present: Presenter | None = None,
        approvals: Any = None,
        carry_out: ApprovalCarrier | None = None,
    ) -> None:
        self._store = store
        self._budget = budget
        self._config = config  # THE live object — enabled and limits read per cycle
        self._skills = list(skills)
        self._events = events
        self._workspace = workspace
        self._workbench = workbench
        self._executor = executor
        # the planning phases never see the full executor — reads only
        self._read_executor = read_only(executor) if executor is not None else None
        self._notify = notify
        self._pause = pause
        self._present = present
        self._approvals = approvals
        self._carry_out = carry_out
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # the anomaly monitor's streaks — ephemeral by design: the pause
        # itself is durable (it lives in config), the counting is not
        self._refuse_streaks: dict[str, int] = {}
        self._discard_streak = 0
        self._paused_note_sent = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="proactive-worker")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    async def run_forever(self) -> None:
        # re-arm: rows a crashed boot left running go back to pending so
        # this boot's first cycle retries them — restart survival is the
        # whole point of persisting the lifecycle in rows
        requeued = await self._store.requeue_running()
        if requeued:
            log.info("re-arming %d proactive task(s) left running by a previous boot", requeued)
        while not self._stop.is_set():
            try:
                ran = await self.cycle()
                if ran:
                    log.info("proactive cycle ran %d task(s)", ran)
            except Exception:
                log.exception("proactive cycle failed — retrying next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.proactive.interval_minutes * 60
                )
        log.info("proactive worker stopped")

    # -- one cycle ---------------------------------------------------------------

    async def cycle(self) -> int:
        """One detect → score → select → run pass. Returns how many tasks
        ran. Every gate is read live, so a chat-made change applies from
        the next cycle without a restart."""
        if not self._config.proactive.enabled:
            return 0
        day = today()
        ran = 0

        # crash recovery first: pending rows run before new detection —
        # their keys are taken, so nothing else can ever pick them up
        for task in await self._store.pending():
            if await self._budget.room(day) <= 0:
                log.info("proactive cycle skipped: %s", await self._budget.exhausted(day))
                return ran
            await self._execute_task(task, day)
            ran += 1

        found = await self._detect()
        if not found:
            return ran

        # deterministic scoring: the skill's own estimate, biased by what
        # the user's feedback said about that skill over the last month
        weights = await self._store.feedback_weights()
        found.sort(
            key=lambda opp: (opp.score * weights.get(opp.skill, 1.0), opp.key),
            reverse=True,
        )
        for opp in found:
            if await self._budget.room(day) <= 0:
                break
            task = await self._store.claim(
                skill=opp.skill,
                key=opp.key,
                label=opp.label,
                opportunity=opp.detail,
                day=day,
            )
            if task is None:
                continue  # the key was taken — this opportunity is already a task
            await self._execute_task(task, day)
            ran += 1
        return ran

    async def _detect(self) -> list[Any]:
        """Ask every enabled skill what it found, concurrently — one
        skill's network/DB calls never wait on another's. A skill that
        crashes or names itself out of the configured list is skipped —
        one broken skill can never stop the others."""
        enabled = [s for s in self._skills if s.name in self._config.proactive.skills]
        results = await asyncio.gather(
            *(s.detect(self._workspace, self._events, self._read_executor) for s in enabled),
            return_exceptions=True,
        )
        found: list[Any] = []
        for skill, result in zip(enabled, results, strict=True):
            if isinstance(result, BaseException):
                log.exception("proactive skill %s detect failed", skill.name, exc_info=result)
                continue
            found.extend(result)
        return found

    async def _execute_task(self, task: ProactiveTask, day: str) -> None:
        """Claim's other half: plan, run, and settle the row. The row says
        the truth at every step — running while the skill works, done or
        failed after, whatever happened in between."""
        skill = next((s for s in self._skills if s.name == task.skill), None)
        if skill is None or skill.name not in self._config.proactive.skills:
            # the skill left the enabled list between claim and run (a
            # /config change mid-cycle) — honest failure, not a silent skip
            await self._store.fail(task.id, "its skill is no longer active")
            return
        await self._store.mark_running(task.id)
        opportunity = Opportunity(
            key=task.opportunity_key,
            skill=task.skill,
            label=task.label,
            detail=task.opportunity,
        )
        try:
            plan = await skill.plan(opportunity, self._workspace, self._read_executor)
            await self._store.set_plan(task.id, plan)
            outcome = await skill.run(task, self._workbench, self._executor)
        except Exception as exc:
            log.exception("proactive task %d (%s) failed", task.id, task.label)
            await self._store.fail(task.id, str(exc)[:200])
            return
        if outcome.refused:
            await self._store.fail(task.id, outcome.summary)
            await self._observe(task.skill, "refused")
            return
        if outcome.failed:
            # the run happened and came back without a result — the row
            # says so, and whatever it truly spent is still charged, so
            # the daily brakes never undercount real work
            await self._store.fail(task.id, outcome.summary)
            await self._budget.charge(
                day,
                tasks=1,
                tokens=outcome.tokens,
                sandbox_seconds=outcome.sandbox_seconds,
            )
            return
        await self._store.finish(task.id, outcome.summary, outcome.provider)
        if outcome.approval_id is not None:
            # the staged action's id joins the plan blob before the result
            # notification ships, so the Approve tap can always find it
            await self._store.link_approval(task.id, outcome.approval_id)
        await self._budget.charge(
            day,
            tasks=1,
            tokens=outcome.tokens,
            sandbox_seconds=outcome.sandbox_seconds,
        )
        await self._observe(task.skill, "ok")
        await self._present_result(task, outcome, day)

    async def _present_result(self, task: ProactiveTask, outcome: RunOutcome, day: str) -> None:
        """Ship the result to the user under the notification cap — the
        same daily-brake discipline as everything else the worker spends.
        A run that staged an action carries all three verdicts; a plain
        result carries the two that mean something (there is nothing to
        revise — the work is done). Refused and failed rows stay in the
        panel: no verdict applies to work that never finished."""
        if self._present is None:
            return
        if not await self._budget.notify_room(day):
            log.info("result notification skipped: today's notification cap is spent")
            return
        await self._budget.charge(day, notifications=1)
        options = (
            ("approve", "revise", "discard")
            if outcome.approval_id is not None
            else ("approve", "discard")
        )
        try:
            await self._present(task.id, outcome.summary, options)
        except Exception:
            log.exception("result notification for task %d failed", task.id)

    # -- the anomaly monitor -------------------------------------------------------

    async def _observe(self, skill: str, outcome: str) -> None:
        """One run outcome into the monitor. 'ok' resets both streaks;
        'refused' builds the per-skill streak; 'discarded' the global one.
        The third in a row trips the auto-pause, once."""
        if outcome == "ok":
            self._refuse_streaks.pop(skill, None)
            self._discard_streak = 0
            return
        if outcome == "refused":
            streak = self._refuse_streaks.get(skill, 0) + 1
            self._refuse_streaks[skill] = streak
            why = f"{skill} keeps proposing actions it isn't allowed to take"
        elif outcome == "discarded":
            self._discard_streak += 1
            streak = self._discard_streak
            why = "too many of its results came back unwanted"
        else:  # pragma: no cover — the vocabulary is the worker's own
            return
        if streak >= _AUTO_PAUSE_STREAK:
            await self._trip_pause(why)

    async def _trip_pause(self, why: str) -> None:
        """Flip the durable switch off and say so once. The flag lives in
        the config store, so a restart never silently clears the pause."""
        if self._paused_note_sent:
            return
        self._paused_note_sent = True
        if self._pause is not None:
            with contextlib.suppress(Exception):
                await self._pause()
        if self._notify is not None:
            with contextlib.suppress(Exception):
                await self._notify(_PAUSE_NOTE)
        log.warning("proactive worker auto-paused: %s", why)

    async def note_feedback(self, skill: str, feedback: str) -> None:
        """A task verdict from outside the cycle — the one-tap buttons on
        a result notification. An approval breaks a streak the same way a
        good run does; a discard builds the global one; revised is a
        correction, not a rejection, and counts toward neither."""
        if feedback == "approved":
            await self._observe(skill, "ok")
        elif feedback == "discarded":
            await self._observe(skill, "discarded")

    # -- the one-tap verdicts on a result ----------------------------------------

    # the button verbs, mapped onto the feedback the row stores — keys
    # always match TASK_VERDICTS, the one source every surface's buttons
    # share, so an added or renamed verb can't desync from here
    _VERDICTS = dict(zip(TASK_VERDICTS, ("approved", "revised", "discarded"), strict=True))

    async def decide_task(self, task_id: int, decision: str) -> str | None:
        """The one-tap verdict on a finished task's result. Returns None
        when the tap applied (its outcome is the acknowledgment — the
        carried-out action's own words, or the button going quiet), or a
        short plain-words note when it didn't, so a stale or double tap
        is never met with silence.

        Approve carries out the task's staged action — but never blindly:
        the skill revalidates live state first, and a draft whose world
        moved on is dropped, not fired. With nothing staged, the verdict
        is the feedback itself: this kind of result was useful. Revise
        parks the task for the user's edited reply and retires the stale
        draft; Discard retires it and says so.
        """
        task = await self._store.get(task_id)
        if task is None:
            return "that task could not be found."
        if task.feedback is not None:
            return "that task was already decided."
        verdict = self._VERDICTS.get(decision)
        if verdict is None:
            return None  # an odd tap — the store's vocabulary is the worker's own
        linked = (task.plan or {}).get("approval_id")

        if verdict == "approved" and linked is not None:
            skill = next((s for s in self._skills if s.name == task.skill), None)
            if skill is None or skill.name not in self._config.proactive.skills:
                return "its skill is no longer active."
            fresh = False
            if hasattr(skill, "revalidate"):
                try:
                    fresh = await skill.revalidate(
                        Opportunity(
                            key=task.opportunity_key,
                            skill=task.skill,
                            label=task.label,
                            detail=task.opportunity,
                        ),
                        self._workspace,
                        self._events,
                    )
                except Exception:
                    log.exception("revalidating task %d before its approve", task_id)
            if not fresh:
                # the world moved on — the staged action must never fire,
                # and the row says why
                await self._store.record_feedback(task_id, "discarded", note="stale")
                await self._kill_approval(linked)
                return "that had moved on, so I dropped the draft — nothing was sent."
            await self._store.record_feedback(task_id, "approved")
            if self._carry_out is None:
                return "that approval could not be carried out here."
            carried = await self._carry_out(linked, "approve")
            if carried is None:
                # fresh by revalidation but no longer decidable — the
                # approval's own expiry clock ran out first
                return "that approval had expired — the action never ran."
            await self._observe(task.skill, "ok")
            return None  # the carried-out action's own words are the acknowledgment

        if verdict == "approved":
            # a plain result — the verdict is the feedback: keep doing this
            await self._store.record_feedback(task_id, "approved")
            await self._observe(task.skill, "ok")
            return "noted — I'll keep doing things like that."

        if verdict == "revised":
            await self._store.record_feedback(task_id, "revised")
            await self._kill_approval(linked)  # the draft as staged is dead
            return "noted — send me the corrected version and I'll take it from there."

        # discard — whatever was staged must never fire
        await self._store.record_feedback(task_id, "discarded")
        await self._kill_approval(linked)
        await self._observe(task.skill, "discarded")
        return "dropped — I'll do less of that."

    async def _kill_approval(self, approval_id: Any) -> None:
        """Retire a task's staged action quietly — decided denied in the
        store so its own one-tap buttons can never fire it later. The
        verdict's acknowledgment comes from the task path, not a second
        dismissal message."""
        if approval_id is None or self._approvals is None:
            return
        with contextlib.suppress(Exception):
            await self._approvals.decide(approval_id, DENIED)
