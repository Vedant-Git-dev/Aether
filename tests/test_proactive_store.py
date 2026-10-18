"""Proactive task store tests.

Integration: real Postgres — the dedup the unique index enforces,
ciphertext at rest, lifecycle transitions, the derived feedback
weights, and restart survival (the point of persisting rows at all).
"""

from __future__ import annotations

import pytest

from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.proactive import APPROVED, DISCARDED, DONE, FAILED, PENDING, REVISED
from aether.proactive.store import ProactiveStore, ProactiveTask


def _make_store(db) -> ProactiveStore:
    return ProactiveStore(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))


async def _claim(store: ProactiveStore, **kwargs) -> ProactiveTask:
    task = await store.claim(**kwargs)
    assert task is not None
    return task


async def _get(store: ProactiveStore, task_id: int) -> ProactiveTask:
    row = await store.get(task_id)
    assert row is not None
    return row


@pytest.mark.integration
async def test_claim_persists_encrypted_and_dedups(db) -> None:
    store = _make_store(db)
    task = await _claim(
        store,
        skill="followups",
        key="thread-123",
        label="reply to the thread",
        opportunity={"thread": "thread-123", "draft": "a secret draft"},
        day="2026-10-17",
    )
    assert task.status == PENDING
    assert task.opportunity["draft"] == "a secret draft"

    # on disk: ciphertext, not the detail
    raw = await db.fetchval("SELECT opportunity_enc FROM proactive_tasks WHERE id = $1", task.id)
    assert b"a secret draft" not in raw

    # the dedup is the unique index: the same (skill, key) never claims twice
    again = await store.claim(
        skill="followups",
        key="thread-123",
        label="reply to the thread",
        opportunity={},
        day="2026-10-17",
    )
    assert again is None

    # a different skill may take the same source, a different key the same skill
    other_skill = await _claim(
        store,
        skill="research",
        key="thread-123",
        label="look it up",
        opportunity={},
        day="2026-10-17",
    )
    other_key = await _claim(
        store,
        skill="followups",
        key="thread-456",
        label="another",
        opportunity={},
        day="2026-10-17",
    )
    assert other_skill.id != other_key.id


@pytest.mark.integration
async def test_lifecycle_transitions_land_on_the_row(db) -> None:
    store = _make_store(db)
    task = await _claim(
        store,
        skill="research",
        key="topic-x",
        label="dig into topic x",
        opportunity={"query": "topic x"},
        day="2026-10-17",
    )
    await store.mark_running(task.id)
    await store.set_plan(task.id, {"steps": ["search", "read", "write"]})
    await store.finish(task.id, "the digest, in plain words", provider="none")

    row = await _get(store, task.id)
    assert row.status == DONE
    assert row.plan is not None and row.plan["steps"] == ["search", "read", "write"]
    assert row.result == "the digest, in plain words"
    assert row.provider == "none"

    # a failure keeps its error the same way a result keeps its text
    bad = await _claim(
        store, skill="media", key="file-9", label="caption it", opportunity={}, day="2026-10-17"
    )
    await store.mark_running(bad.id)
    await store.fail(bad.id, "the sandbox refused the job")
    failed = await _get(store, bad.id)
    assert failed.status == FAILED
    assert failed.result == "the sandbox refused the job"


@pytest.mark.integration
async def test_link_approval_merges_into_the_encrypted_plan(db) -> None:
    store = _make_store(db)
    task = await _claim(
        store, skill="followups", key="k", label="k", opportunity={}, day="2026-10-17"
    )
    await store.set_plan(task.id, {"step": "send the draft"})
    await store.link_approval(task.id, 9)
    row = await _get(store, task.id)
    assert row.plan == {"step": "send the draft", "approval_id": 9}

    raw = await db.fetchval("SELECT plan_enc FROM proactive_tasks WHERE id = $1", task.id)
    assert b"approval_id" not in raw  # encrypted, same trade as the rest of the blob

    # a task that never planned merges into an empty blob all the same
    bare = await _claim(
        store, skill="research", key="bare", label="b", opportunity={}, day="2026-10-17"
    )
    await store.link_approval(bare.id, 4)
    assert (await _get(store, bare.id)).plan == {"approval_id": 4}

    # a missing row is a no-op, not a crash — the notification already shipped
    await store.link_approval(404, 1)


@pytest.mark.integration
async def test_pending_and_requeue_running_arms_the_queue(db) -> None:
    store = _make_store(db)
    ready = await _claim(
        store, skill="followups", key="a", label="a", opportunity={}, day="2026-10-17"
    )
    other = await _claim(
        store, skill="followups", key="b", label="b", opportunity={}, day="2026-10-17"
    )
    await store.finish(other.id, "already settled — not the queue's business")

    # a crashed boot leaves a row running — the re-arm takes it back
    await store.mark_running(ready.id)
    assert await store.requeue_running() == 1
    assert [t.id for t in await store.pending()] == [ready.id]
    assert (await _get(store, ready.id)).status == PENDING


@pytest.mark.integration
async def test_record_feedback_validates_and_encrypts_the_note(db) -> None:
    store = _make_store(db)
    task = await _claim(
        store, skill="research", key="k", label="k", opportunity={}, day="2026-10-17"
    )
    await store.record_feedback(task.id, DISCARDED, note="not what I asked for")
    row = await _get(store, task.id)
    assert row.feedback == DISCARDED
    assert row.feedback_note == "not what I asked for"

    raw = await db.fetchval("SELECT feedback_note_enc FROM proactive_tasks WHERE id = $1", task.id)
    assert b"not what I asked for" not in raw

    with pytest.raises(ValueError):
        await store.record_feedback(task.id, "meh")  # outside the vocabulary

    # a bare verdict clears nothing — a new note replaces the old one
    await store.record_feedback(task.id, APPROVED)
    row = await _get(store, task.id)
    assert row.feedback == APPROVED
    assert row.feedback_note is None


@pytest.mark.integration
async def test_feedback_weights_derive_from_the_window(db) -> None:
    store = _make_store(db)
    tasks = [
        ("research", APPROVED),
        ("research", APPROVED),
        ("research", DISCARDED),
        ("media", DISCARDED),
        ("media", DISCARDED),
        ("media", DISCARDED),
        ("media", DISCARDED),
        ("media", DISCARDED),
        ("followups", REVISED),
    ]
    for i, (skill, feedback) in enumerate(tasks):
        task = await _claim(
            store, skill=skill, key=f"k{i}", label=str(i), opportunity={}, day="2026-10-17"
        )
        await store.finish(task.id, f"result {i}")
        await store.record_feedback(task.id, feedback)

    weights = await store.feedback_weights()
    assert weights["research"] == pytest.approx(1.0 + 0.15 * (2 - 1))
    assert weights["media"] == pytest.approx(0.25)  # 1 - 0.75 clamps at a quarter
    assert weights["followups"] == pytest.approx(1.0)  # revised counts toward neither streak


@pytest.mark.integration
async def test_rows_survive_a_restart(db) -> None:
    """A brand-new store over the same database reads what the old one
    wrote — nothing carries over but the rows (and the key)."""
    first = _make_store(db)
    task = await _claim(
        first,
        skill="followups",
        key="thread-789",
        label="reply while I was down",
        opportunity={"draft": "hello again"},
        day="2026-10-17",
    )
    await first.mark_running(task.id)
    await first.set_plan(task.id, {"step": "send"})

    second = ProactiveStore(db, first._cipher, AuditLog(db))
    assert await second.requeue_running() == 1
    row = await _get(second, task.id)
    assert row.status == PENDING
    assert row.opportunity["draft"] == "hello again"
    assert row.plan == {"step": "send"}

    # and the dedup survives too: the key stays taken across the restart
    assert (
        await second.claim(
            skill="followups",
            key="thread-789",
            label="reply while I was down",
            opportunity={},
            day="2026-10-18",
        )
        is None
    )
