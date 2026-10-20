"""Proactive worker tests — unit, against the fake store.

The cycle's decisions, not the database's (the real store's rows are
covered in test_proactive_store.py): the kill switch, the budget gates,
deterministic scoring under feedback weights, the dedup the store
enforces, crash-recovery ordering, and the anomaly monitor that pauses
the worker — durably, and says so once.
"""

from __future__ import annotations

from fakes import FakeMediaStore, FakeProactiveStore

from aether.authz.approvals import DENIED
from aether.config import AppConfig, ProactiveConfig
from aether.connectors.base import Attachment
from aether.counters import today
from aether.llm.types import ToolCall
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
    a fresh key each time), swap `outcome` to steer the monitor, and flip
    `fresh` to steer an Approve's revalidation."""

    name = "followups"

    def __init__(self, *, found=None, outcome=None) -> None:
        self.found = list(found or [])
        self.outcome = outcome or RunOutcome(summary="the result", tokens=100, sandbox_seconds=30)
        self.fresh = True  # what revalidate answers when a task is approved
        self.detect_calls = 0
        self.plan_calls: list[Opportunity] = []
        self.run_calls: list[object] = []
        self.revalidate_calls: list[Opportunity] = []

    async def detect(self, profile, events, executor=None) -> list[Opportunity]:
        self.detect_calls += 1
        found = self.found
        self.found = []
        return found

    async def plan(self, opportunity, profile, executor=None) -> dict:
        self.plan_calls.append(opportunity)
        return {"step": "go"}

    async def run(self, task, workbench, executor) -> RunOutcome:
        self.run_calls.append(task)
        return self.outcome

    async def revalidate(self, opportunity, profile, events) -> bool:
        self.revalidate_calls.append(opportunity)
        return self.fresh


def _config(**proactive) -> AppConfig:
    return AppConfig(proactive=ProactiveConfig(enabled=True, **proactive))


def _worker(
    store,
    counters,
    config,
    skills,
    *,
    notify=None,
    pause=None,
    executor=None,
    present=None,
    approvals=None,
    carry_out=None,
    media=None,
) -> ProactiveWorker:
    return ProactiveWorker(
        store=store,
        budget=Budget(counters, config.proactive),
        config=config,
        skills=skills,
        executor=executor,
        notify=notify,
        pause=pause,
        present=present,
        approvals=approvals,
        carry_out=carry_out,
        media=media,
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
        async def detect(self, profile, events, executor=None):
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
        async def detect(self, profile, events, executor=None):
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
# the phase gate: planning reads only, run gets the full executor
# ---------------------------------------------------------------------------


async def test_planning_runs_read_only_and_run_gets_the_full_executor() -> None:
    """The worker hands detect and plan an executor that refuses writes
    in plain words before the policy is even asked; run receives the
    full one, where the policy (not the phase) is the only gate."""
    store, counters = FakeProactiveStore(), FakeCounters()
    seen: dict[str, str] = {}

    write = ToolCall(id="w", name="workspace_remember", arguments={"text": "n", "kind": "fact"})

    async def full(call):
        return "the write ran"

    class Probe(FakeSkill):
        async def detect(self, profile, events, executor=None):
            assert executor is not None  # the worker always hands one over
            seen["detect"] = await executor(write)
            return [Opportunity(key="k", skill="followups", label="x", score=5.0)]

        async def plan(self, opportunity, profile, executor=None):
            assert executor is not None
            seen["plan"] = await executor(write)
            return {"step": "go"}

        async def run(self, task, workbench, executor):
            assert executor is not None
            seen["run"] = await executor(write)
            return self.outcome

    worker = _worker(store, counters, _config(), [Probe()], executor=full)
    assert await worker.cycle() == 1
    assert seen["detect"].startswith("not run — planning only looks things up")
    assert seen["plan"].startswith("not run — planning only looks things up")
    assert seen["run"] == "the write ran"
    assert store.tasks[0].status == DONE


async def test_look_at_media_passes_the_planning_gate() -> None:
    """look_at_media reads stored bytes and writes nothing new, so the
    planning phases may use it — the gate decides by name, and it is in
    READ_TOOLS."""
    from aether.proactive.worker import read_only

    async def full(call: ToolCall) -> str:
        return "a ginger cat on a windowsill"

    guarded = read_only(full)  # type: ignore[arg-type] — the fake speaks the same shape
    look = ToolCall(id="l", name="look_at_media", arguments={"media_id": 3})
    assert await guarded(look) == "a ginger cat on a windowsill"


async def test_edit_video_is_refused_in_planning_and_allowed_in_run() -> None:
    """edit_video writes a new stored blob, so planning may not touch it
    — the gate decides by name, and it is deliberately absent from
    READ_TOOLS."""
    from aether.proactive.worker import read_only

    async def full(call: ToolCall) -> str:
        return "The edit is done — the new copy is stored as media id 7."

    guarded = read_only(full)  # type: ignore[arg-type] — the fake speaks the same shape
    edit = ToolCall(id="e", name="edit_video", arguments={"media_id": 3, "operation": "mute"})
    assert (await guarded(edit)).startswith("not run — planning only looks things up")
    # the run phase's executor is the full one — the policy gates, not the phase
    assert (await full(edit)).startswith("The edit is done")


async def test_share_media_is_refused_in_planning_and_allowed_in_run() -> None:
    """A planning phase sends nothing — a file going out is a change, and
    changes wait for the run phase. share_media is deliberately absent
    from READ_TOOLS even though it only reaches the owner's own chats."""
    from aether.proactive.worker import read_only

    async def full(call: ToolCall) -> str:
        return "Sent to the user's chat surfaces."

    guarded = read_only(full)  # type: ignore[arg-type] — the fake speaks the same shape
    share = ToolCall(id="s", name="share_media", arguments={"media_id": 3, "text": "here"})
    assert (await guarded(share)).startswith("not run — planning only looks things up")
    # the run phase's executor is the full one — the phase gate, not the policy
    assert (await full(share)).startswith("Sent to the user")


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
        async def plan(self, opportunity, profile, executor=None):
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

        async def detect(self, profile, events, executor=None):
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


# ---------------------------------------------------------------------------
# result notifications: the cap, the options, and what never presents
# ---------------------------------------------------------------------------


def _present_recorder():
    shown: list[tuple[int, str, tuple[str, ...]]] = []

    async def present(task_id: int, text: str, options) -> None:
        shown.append((task_id, text, tuple(options)))

    return present, shown


async def _done_row(store, *, approval_id=None, plan=None) -> None:
    """A finished row the way the cycle leaves one: claimed, planned, done."""
    task = await store.claim(skill="followups", key="k", label="x", opportunity={}, day=today())
    assert task is not None
    merged = dict(plan or {})
    if approval_id is not None:
        merged["approval_id"] = approval_id
    await store.set_plan(task.id, merged)
    await store.finish(task.id, "the result")


async def test_a_finished_result_presents_under_the_cap() -> None:
    day = today()
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=5.0)])
    present, shown = _present_recorder()
    worker = _worker(store, counters, _config(), [skill], present=present)
    assert await worker.cycle() == 1
    assert shown == [(1, "the result", ("approve", "discard"))]  # nothing staged
    assert counters.values[(day, "notifications")] == 1


async def test_a_staged_action_presents_all_three_verdicts_and_links_it() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill(
        found=[Opportunity(key="k", skill="followups", label="x", score=5.0)],
        outcome=RunOutcome(summary="drafted", approval_id=9),
    )
    present, shown = _present_recorder()
    worker = _worker(store, counters, _config(), [skill], present=present)
    assert await worker.cycle() == 1
    # the id rode the outcome into the plan blob before anything shipped
    assert store.linked == [(1, 9)]
    assert store.tasks[0].plan == {"step": "go", "approval_id": 9}
    assert shown == [(1, "drafted", ("approve", "revise", "discard"))]


async def test_a_spent_notification_cap_skips_the_presentation() -> None:
    day = today()
    store = FakeProactiveStore()
    counters = FakeCounters(values={(day, "notifications"): 5})  # the default cap
    skill = FakeSkill(found=[Opportunity(key="k", skill="followups", label="x", score=5.0)])
    present, shown = _present_recorder()
    worker = _worker(store, counters, _config(), [skill], present=present)
    assert await worker.cycle() == 1
    assert shown == []  # still done, still charged for the work itself
    assert counters.values[(day, "notifications")] == 5  # nothing extra spent
    assert counters.bumps.count((day, "notifications", 1)) == 0


async def test_a_failed_or_refused_run_never_presents() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    present, shown = _present_recorder()
    refused = FakeSkill(
        found=[Opportunity(key="r", skill="followups", label="x", score=9.0)],
        outcome=RunOutcome(summary="parked", refused=True),
    )
    failed = FakeSkill(
        found=[Opportunity(key="f", skill="followups", label="y", score=1.0)],
        outcome=RunOutcome(summary="the job died", failed=True),
    )
    worker = _worker(store, counters, _config(), [refused, failed], present=present)
    assert await worker.cycle() == 2
    assert shown == []  # no verdict applies to work that never finished
    assert all(t.status == FAILED for t in store.tasks)


async def test_a_result_with_stored_media_sends_the_file_with_the_words() -> None:
    """A task row that carries a media id ships the file itself with the
    result — the presentation seam (present_result) accepts the
    attachment only when the bytes are still in the store."""
    store, counters = FakeProactiveStore(), FakeCounters()
    media_store = FakeMediaStore()
    clip = b"\x00\x00\x00\x18ftypisom" + b"laptop-tour"
    kept = await media_store.put(clip, filename="laptop.mp4", origin="edit")
    task = await store.claim(skill="followups", key="k", label="x", opportunity={}, day=today())
    assert task is not None
    task.media_id = kept.media_id
    received: list[dict] = []

    async def present(task_id: int, text: str, options, **kwargs) -> None:
        received.append({"task_id": task_id, "text": text, "options": tuple(options), **kwargs})

    worker = _worker(store, counters, _config(), [FakeSkill()], present=present, media=media_store)
    await worker._present_result(task, RunOutcome(summary="the captioned clip"), today())

    assert received == [
        {
            "task_id": task.id,
            "text": "the captioned clip",
            "options": ("approve", "discard"),
            "attachment": Attachment("video", "laptop.mp4", "video/mp4", clip),
        }
    ]


async def test_a_result_whose_blob_is_gone_says_so_and_sends_the_words() -> None:
    """Eviction between the run and the presentation never raises — the
    words go alone, with a plain note standing in for the file."""
    store, counters = FakeProactiveStore(), FakeCounters()
    media_store = FakeMediaStore()
    kept = await media_store.put(b"ID3" + b"x" * 64, filename="voice.mp3", origin="audio")
    task = await store.claim(skill="followups", key="k", label="x", opportunity={}, day=today())
    assert task is not None
    task.media_id = kept.media_id
    media_store.rows.pop(kept.media_id)  # evicted to make room, say
    received: list[dict] = []

    async def present(task_id: int, text: str, options, **kwargs) -> None:
        received.append({"task_id": task_id, "text": text, "options": tuple(options), **kwargs})

    worker = _worker(store, counters, _config(), [FakeSkill()], present=present, media=media_store)
    await worker._present_result(task, RunOutcome(summary="the digest"), today())

    assert received == [
        {
            "task_id": task.id,
            "text": "the digest\n\n(the file that went with this is no longer stored)",
            "options": ("approve", "discard"),
        }
    ]


async def test_a_result_without_a_media_store_wired_sends_words_only() -> None:
    """No media store (a bare worker, an older wiring) — the words go out
    exactly as before, no crash on the media_id the row carries."""
    store, counters = FakeProactiveStore(), FakeCounters()
    task = await store.claim(skill="followups", key="k", label="x", opportunity={}, day=today())
    assert task is not None
    task.media_id = 9
    present, shown = _present_recorder()

    worker = _worker(store, counters, _config(), [FakeSkill()], present=present)
    await worker._present_result(task, RunOutcome(summary="the digest"), today())

    assert shown == [(task.id, "the digest", ("approve", "discard"))]


# ---------------------------------------------------------------------------
# the one-tap verdicts: approve (fresh and stale), revise, discard
# ---------------------------------------------------------------------------


class FakeApprovals:
    """The approval store's decide: recorded, never consulted further —
    the worker only ever retires a staged action with a denial."""

    def __init__(self) -> None:
        self.decisions: list[tuple[int, str]] = []

    async def decide(self, approval_id, decision, actor="owner") -> None:
        self.decisions.append((approval_id, decision))


def _carry_recorder(answer: str | None = "done"):
    carried: list[tuple[int, str]] = []

    async def carry_out(approval_id: int, decision: str):
        carried.append((approval_id, decision))
        return answer

    return carry_out, carried


async def test_a_missing_task_says_so() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    worker = _worker(store, counters, _config(), [FakeSkill()])
    assert await worker.decide_task(404, "approve") == "that task could not be found."


async def test_an_odd_verb_answers_nothing() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    await _done_row(store)
    worker = _worker(store, counters, _config(), [FakeSkill()])
    assert await worker.decide_task(1, "explode") is None


async def test_a_second_tap_on_a_decided_task_says_so() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    await _done_row(store)
    worker = _worker(store, counters, _config(), [FakeSkill()])
    assert await worker.decide_task(1, "approve") is not None
    assert await worker.decide_task(1, "discard") == "that task was already decided."
    assert store.tasks[0].feedback == "approved"  # the first tap still stands


async def test_approve_carries_out_a_fresh_staged_action() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill()
    approvals = FakeApprovals()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(store, counters, _config(), [skill], approvals=approvals, carry_out=carry_out)
    assert await worker.decide_task(1, "approve") is None  # the outcome is the ack
    assert carried == [(9, "approve")]
    assert len(skill.revalidate_calls) == 1  # never fired blindly
    assert store.tasks[0].feedback == "approved"
    assert approvals.decisions == []  # the staged action fired, not died


async def test_approve_of_a_moved_on_world_drops_the_draft() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    skill = FakeSkill()
    skill.fresh = False  # revalidate says the thread already moved on
    approvals = FakeApprovals()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(store, counters, _config(), [skill], approvals=approvals, carry_out=carry_out)
    note = await worker.decide_task(1, "approve")
    assert note == "that had moved on, so I dropped the draft — nothing was sent."
    assert carried == []  # nothing was sent
    task = store.tasks[0]
    assert task.feedback == "discarded" and task.feedback_note == "stale"
    assert approvals.decisions == [(9, DENIED)]  # its buttons can never fire it


async def test_approve_of_a_revalidate_crash_counts_as_stale() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    approvals = FakeApprovals()

    class Exploding(FakeSkill):
        async def revalidate(self, opportunity, profile, events):
            raise RuntimeError("the connector is down")

    skill = Exploding()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(store, counters, _config(), [skill], approvals=approvals, carry_out=carry_out)
    note = await worker.decide_task(1, "approve")
    assert note == "that had moved on, so I dropped the draft — nothing was sent."
    assert carried == []  # fail-safe: an unverifiable action never fires
    assert store.tasks[0].feedback == "discarded"


async def test_approve_of_an_expired_staged_action_never_fires() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    approvals = FakeApprovals()
    carry_out, carried = _carry_recorder(answer=None)  # the approval store refused
    await _done_row(store, approval_id=9)
    worker = _worker(
        store, counters, _config(), [FakeSkill()], approvals=approvals, carry_out=carry_out
    )
    note = await worker.decide_task(1, "approve")
    assert note == "that approval had expired — the action never ran."
    assert carried == [(9, "approve")]  # asked, refused — nothing fired


async def test_approve_without_a_staged_action_is_just_feedback() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    carry_out, carried = _carry_recorder()
    await _done_row(store)  # a plain result — nothing staged
    worker = _worker(store, counters, _config(), [FakeSkill()], carry_out=carry_out)
    assert await worker.decide_task(1, "approve") == "noted — I'll keep doing things like that."
    assert carried == []  # there was nothing to carry out
    assert store.tasks[0].feedback == "approved"


async def test_revise_parks_the_task_and_retires_the_stale_draft() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    approvals = FakeApprovals()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(
        store, counters, _config(), [FakeSkill()], approvals=approvals, carry_out=carry_out
    )
    note = await worker.decide_task(1, "revise")
    assert note == "noted — send me the corrected version and I'll take it from there."
    assert carried == []
    assert store.tasks[0].feedback == "revised"
    assert approvals.decisions == [(9, DENIED)]  # the draft as staged is dead


async def test_discard_drops_everything_and_records_it() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    approvals = FakeApprovals()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(
        store, counters, _config(), [FakeSkill()], approvals=approvals, carry_out=carry_out
    )
    assert await worker.decide_task(1, "discard") == "dropped — I'll do less of that."
    assert carried == []
    assert store.tasks[0].feedback == "discarded"
    assert approvals.decisions == [(9, DENIED)]  # whatever was staged never fires


async def test_approve_of_a_retired_skill_never_fires() -> None:
    store, counters = FakeProactiveStore(), FakeCounters()
    carry_out, carried = _carry_recorder()
    await _done_row(store, approval_id=9)
    worker = _worker(store, counters, _config(skills=[]), [FakeSkill()], carry_out=carry_out)
    assert await worker.decide_task(1, "approve") == "its skill is no longer active."
    assert carried == []
    assert store.tasks[0].feedback is None  # the row stays open, nothing recorded
