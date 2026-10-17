"""Proactive worker tests — unit, against the fake store.

The cycle's decisions, not the database's (the real store's rows are
covered in test_proactive_store.py): the kill switch, the budget gates,
deterministic scoring under feedback weights, the dedup the store
enforces, crash-recovery ordering, and the anomaly monitor that pauses
the worker — durably, and says so once.
"""

from __future__ import annotations

from fakes import FakeProactiveStore

from aether.config import AppConfig, ProactiveConfig
from aether.counters import today
from aether.proactive import DISCARDED, DONE, FAILED, PENDING, Budget, Opportunity, ProactiveWorker
from aether.proactive.worker import _PAUSE_NOTE, RunOutcome


class FakeCounters:
    """The counter surface Budget needs — in-memory, per (day, field)."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})
        self.bumps: list[tuple[str, str, int]] = []

    async def value(self, day: str, field: str) -> int:
        return self.values.get((day, field), 0)

    async def bump(self, day: str, field: str, n: int = 1) -> int:
        self.bumps.append((day, field, n))
        key = (day, field)
        self.values[key] = self.values.get(key, 0) + n
        return self.values[key]


class FakeSkill:
    """A skill double: a detect list it hands out once, a plan, and one
    run outcome. Tests re-script `found` between cycles (streak tests need
    a fresh key each time) and swap `outcome` to steer the monitor."""

    name = "followups"

    def __init__(self, *, found=None, outcome=None) -> None:
        self.found = list(found or [])
        self.outcome = outcome or RunOutcome(summary="the result", tokens=100, sandbox_seconds=30)
        self.detect_calls = 0
        self.plan_calls: list[Opportunity] = []
        self.run_calls: list[object] = []

    async def detect(self, profile, events) -> list[Opportunity]:
        self.detect_calls += 1
        found = self.found
        self.found = []
        return found

    async def plan(self, opportunity, profile) -> dict:
        self.plan_calls.append(opportunity)
        return {"step": "go"}

    async def run(self, task, workbench, executor) -> RunOutcome:
        self.run_calls.append(task)
        return self.outcome


def _config(**proactive) -> AppConfig:
    return AppConfig(proactive=ProactiveConfig(enabled=True, **proactive))


def _worker(store, counters, config, skills, *, notify=None, pause=None) -> ProactiveWorker:
    return ProactiveWorker(
        store=store,
        budget=Budget(counters, config.proactive),
        config=config,
        skills=skills,
        notify=notify,
        pause=pause,
    )


def _recorders():
    notes: list[str] = []
    pauses: list[bool] = []

    async def notify(text: str) -> None:
        notes.append(text)

    async def pause() -> None:
        pauses.append(True)

    return notify, pause, notes, pauses


# ---------------------------------------------------------------------------
# the kill switch and the budget gates
# ---------------------------------------------------------------------------


async def test_disabled_config_runs_nothing() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=5.0)])
    worker = _worker(store, counters, AppConfig(), [skill])  # enabled=False
    assert await worker.cycle() == 0
    assert skill.detect_calls == 0
    assert store.claims == []


async def test_flipping_enabled_off_stops_the_next_cycle() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill()
    config = _config()
    worker = _worker(store, counters, config, [skill])
    config.proactive.enabled = False  # read live, no restart
    assert await worker.cycle() == 0
    assert skill.detect_calls == 0


async def test_an_exhausted_budget_claims_nothing() -> None:
    day = today()
    store = FakeProactiveStore()
    counters = FakeCounters(values={(day, "tokens"): 100_000})
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=5.0)])
    worker = _worker(store, counters, _config(), [skill])
    assert await worker.cycle() == 0
    assert store.claims == []
    assert skill.run_calls == []


async def test_room_shrinks_as_tasks_charge() -> None:
    day = today()
    counters = FakeCounters()
    budget = Budget(counters, ProactiveConfig(enabled=True, max_tasks_per_day=2))
    assert await budget.room(day) == 2
    await budget.charge(day, tasks=1, tokens=10, sandbox_seconds=5)
    assert counters.bumps == [(day, "tasks", 1), (day, "tokens", 10), (day, "sandbox_seconds", 5)]
    assert await budget.room(day) == 1
    # a zero-cost part of a task charges nothing
    await budget.charge(day, tokens=0, sandbox_seconds=0)
    assert len(counters.bumps) == 3
    assert await budget.exhausted(day) is None
    # any spent cap empties the room, fail-safe
    await budget.charge(day, tasks=1)
    assert await budget.room(day) == 0
    assert (await budget.exhausted(day)) == "today's task budget"


# ---------------------------------------------------------------------------
# detect, score, select — deterministic
# ---------------------------------------------------------------------------


async def test_the_happy_path_claims_plans_runs_and_charges() -> None:
    day = today()
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="the draft", score=7.0)])
    worker = _worker(store, counters, _config(), [skill])
    assert await worker.cycle() == 1

    assert store.claims == [
        {"skill": "followups", "key": "k", "label": "the draft", "day": day, "actor": "worker"}
    ]
    task = store.tasks[0]
    assert task.status == DONE
    assert task.result == "the result"
    assert task.provider is None  # the outcome named no provider
    assert task.plan == {"step": "go"}
    assert len(skill.plan_calls) == 1 and skill.plan_calls[0].key == "k"
    assert counters.values == {
        (day, "tasks"): 1,
        (day, "tokens"): 100,
        (day, "sandbox_seconds"): 30,
    }


async def test_selection_orders_by_score_times_weight_and_caps_at_room() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    store.weights = {"research": 2.0}  # a month of approvals
    hot = FakeSkill(found=[Opportunity(key="low", skill="research", label="weighted", score=5.0)])
    hot.name = "research"  # a second skill with its own identity
    plain = FakeSkill(found=[Opportunity(key="high", skill="followups", label="plain", score=8.0)])
    worker = _worker(store, counters, _config(max_tasks_per_day=1), [plain, hot])
    assert await worker.cycle() == 1  # room for one

    # 5 x 2.0 beats 8 x 1.0, and the loser was never claimed
    assert [c["key"] for c in store.claims] == ["low"]
    assert store.tasks[0].skill == "research"


async def test_a_taken_key_never_claims_twice() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    opp = Opportunity(key="k", skill="followups", label="x", score=5.0)
    skill = FakeSkill(found=[opp])
    worker = _worker(store, counters, _config(), [skill])
    assert await worker.cycle() == 1
    skill.found = [opp]  # re-detected next cycle
    assert await worker.cycle() == 0  # the key is taken — nothing new runs
    assert len(store.claims) == 2  # the claim was attempted and refused


async def test_a_broken_skill_never_stops_the_others() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()

    class Exploding(FakeSkill):
        async def detect(self, profile, events):
            raise RuntimeError("the connector is down")

    broken = Exploding()
    fine = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=1.0)])
    worker = _worker(store, counters, _config(), [broken, fine])
    assert await worker.cycle() == 1
    assert store.tasks[0].status == DONE


async def test_a_skill_outside_the_configured_list_is_skipped() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=5.0)])
    worker = _worker(store, counters, _config(skills=[]), [skill])
    assert await worker.cycle() == 0
    assert skill.detect_calls == 0


# ---------------------------------------------------------------------------
# crash recovery: pending rows run before new detection
# ---------------------------------------------------------------------------


async def test_pending_rows_run_before_new_detection() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    # a row a crashed boot left behind — its key is taken forever
    requeued = await store.claim(
        skill="followups", key="old", label="the stranded one", opportunity={}, day=today()
    )
    assert requeued is not None
    order: list[str] = []

    class Ordered(FakeSkill):
        async def detect(self, profile, events):
            order.append("detect")
            return [Opportunity(key="new", skill="followups", label="fresh", score=9.0)]

        async def run(self, task, workbench, executor):
            order.append(f"run:{task.opportunity_key}")
            return self.outcome

    skill = Ordered()
    worker = _worker(store, counters, _config(), [skill])
    assert await worker.cycle() == 2
    assert order == ["run:old", "detect", "run:new"]  # recovery first, always


async def test_run_forever_rearms_running_rows_on_boot() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    task = await store.claim(skill="followups", key="k", label="k", opportunity={}, day=today())
    assert task is not None
    await store.mark_running(task.id)  # what a crash mid-run leaves behind
    worker = _worker(store, counters, _config(max_tasks_per_day=0), [FakeSkill()])
    worker.start()
    await worker.stop()  # stop awaits the task, so the boot steps have run
    assert store.tasks[0].status == PENDING  # back in the queue, not stranded
    assert len(store.claims) == 1  # the seed — the re-armed row was requeued, not re-claimed


# ---------------------------------------------------------------------------
# failures, refusals, and the skill leaving the list mid-flight
# ---------------------------------------------------------------------------


async def test_a_refused_run_fails_the_row() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(
        found=[Opportunity(key="k", skill="followups", label="x", score=5.0)],
        outcome=RunOutcome(summary="the action parked for approval", refused=True),
    )
    worker = _worker(store, counters, _config(), [skill])
    assert await worker.cycle() == 1
    task = store.tasks[0]
    assert task.status == FAILED
    assert task.result == "the action parked for approval"
    assert counters.values == {}  # a refused run charges nothing


async def test_a_plan_or_run_crash_fails_the_row_without_stopping_the_cycle() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    boom = FakeSkill(found=[Opportunity(key="boom", skill="followups", label="x", score=9.0)])
    boom.outcome = RunOutcome(summary="ok")

    class BadPlan(FakeSkill):
        async def plan(self, opportunity, profile):
            raise RuntimeError("planning exploded")

    bad_plan = BadPlan(found=[Opportunity(key="bad", skill="media", label="y", score=1.0)])
    bad_plan.name = "media"  # its own identity, or the worker would match boom's
    worker = _worker(store, counters, _config(), [bad_plan, boom])
    assert await worker.cycle() == 2
    failed = next(t for t in store.tasks if t.opportunity_key == "bad")
    assert failed.status == FAILED
    assert failed.result is not None and "planning exploded" in failed.result
    assert next(t for t in store.tasks if t.opportunity_key == "boom").status == DONE


async def test_a_skill_disabled_between_claim_and_run_fails_honestly() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    config = _config()

    class Vanishing(FakeSkill):
        def __init__(self) -> None:
            super().__init__()
            self.config = config

        async def detect(self, profile, events):
            self.config.proactive.skills = []  # a /config change mid-cycle
            return [Opportunity(key="k", skill="followups", label="x", score=5.0)]

    worker = _worker(store, counters, config, [Vanishing()])
    assert await worker.cycle() == 1
    task = store.tasks[0]
    assert task.status == FAILED
    assert task.result == "its skill is no longer active"


# ---------------------------------------------------------------------------
# the anomaly monitor: three in a row pauses the worker, once
# ---------------------------------------------------------------------------


async def test_three_refusals_from_one_skill_pause_once_and_say_so_once() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(outcome=RunOutcome(summary="refused", refused=True))
    notify, pause, notes, pauses = _recorders()
    worker = _worker(store, counters, _config(), [skill], notify=notify, pause=pause)
    for i in range(3):
        skill.found = [Opportunity(key=f"k{i}", skill="followups", label="x", score=1.0)]
        await worker.cycle()
    assert pauses == [True]
    assert notes == [_PAUSE_NOTE]
    assert len([t for t in store.tasks if t.status == FAILED]) == 3

    # a fourth refusal changes nothing — the pause was already said once
    skill.found = [Opportunity(key="k4", skill="followups", label="x", score=1.0)]
    await worker.cycle()
    assert pauses == [True]
    assert notes == [_PAUSE_NOTE]


async def test_a_good_run_resets_the_refusal_streak() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(outcome=RunOutcome(summary="refused", refused=True))
    notify, pause, notes, pauses = _recorders()
    worker = _worker(store, counters, _config(), [skill], notify=notify, pause=pause)
    for i in range(2):
        skill.found = [Opportunity(key=f"r{i}", skill="followups", label="x", score=1.0)]
        await worker.cycle()
    skill.outcome = RunOutcome(summary="the result")  # the streak resets
    skill.found = [Opportunity(key="ok", skill="followups", label="x", score=1.0)]
    await worker.cycle()
    for i in range(2):  # two more refusals — still under three in a row
        skill.found = [Opportunity(key=f"r2{i}", skill="followups", label="x", score=1.0)]
        await worker.cycle()
    assert pauses == [] and notes == []


async def test_three_discards_in_a_row_trip_the_pause() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    notify, pause, notes, pauses = _recorders()
    worker = _worker(store, counters, _config(), [FakeSkill()], notify=notify, pause=pause)
    for _ in range(2):
        await worker.note_feedback("research", DISCARDED)
    assert pauses == []
    await worker.note_feedback("research", "approved")  # resets the streak
    for _ in range(2):
        await worker.note_feedback("research", DISCARDED)
    assert pauses == []
    await worker.note_feedback("research", DISCARDED)
    assert pauses == [True]
    assert notes == [_PAUSE_NOTE]
    # a fourth discard stays quiet
    await worker.note_feedback("research", DISCARDED)
    assert pauses == [True] and notes == [_PAUSE_NOTE]


async def test_revised_feedback_counts_toward_neither_streak() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    notify, pause, notes, pauses = _recorders()
    worker = _worker(store, counters, _config(), [FakeSkill()], notify=notify, pause=pause)
    for _ in range(10):
        await worker.note_feedback("research", "revised")
    assert pauses == [] and notes == []
