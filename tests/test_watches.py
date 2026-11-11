"""Watches: deterministic matching, honest rendering, sealed at rest.

The matcher is pure code over one event, which is the whole point — no
model decides whether a watch fires. These run without a database except
the store tests, which prove the spec is ciphertext and that only the
owner's origin can arm one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aether.authz.audit import AuditLog
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import Event
from aether.memory.watches import (
    ACTIVE,
    PAUSED,
    Watch,
    WatchSpec,
    WatchStore,
    has_criteria,
    render_alert,
    watch_matches,
)

NOW = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)
SECRET = "AETHER-WATCH-CANARY-91c2"


def _event(
    *,
    event_id: int = 1,
    source: str = "gmail",
    kind: str = "mail",
    payload: dict | None = None,
    urgency: int = 0,
    needs_reply: bool = False,
    noise: bool = False,
    summary: str = "",
    deadline_at: datetime | None = None,
) -> Event:
    return Event(
        id=event_id,
        source=source,
        kind=kind,
        occurred_at=NOW,
        payload=payload or {},
        salience_score=0.0,
        memorable=True,
        meta={},
        urgency=urgency,
        needs_reply=needs_reply,
        noise=noise,
        summary=summary,
        deadline_at=deadline_at,
    )


def _mail(**kwargs) -> Event:
    payload = {
        "subject": "Quarterly invoice",
        "sender": "Rahul Sharma <rahul@example.com>",
        "to": "me@example.com",
        "snippet": "Please pay the invoice before Friday.",
    }
    payload.update(kwargs.pop("payload", {}))
    return _event(payload=payload, kind="mail", **kwargs)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def test_a_mail_watch_matches_the_raw_sender_address() -> None:
    spec = WatchSpec(label="rahul", scope="mail", sender="rahul@example.com")

    assert watch_matches(spec, _mail()) is True


def test_a_mail_watch_matches_a_display_name() -> None:
    spec = WatchSpec(label="rahul", scope="mail", sender="Rahul")

    assert watch_matches(spec, _mail()) is True


def test_a_different_sender_does_not_match() -> None:
    spec = WatchSpec(label="rahul", scope="mail", sender="someone@else.example")

    assert watch_matches(spec, _mail()) is False


def test_a_chat_watch_uses_the_encrypted_sender_block() -> None:
    event = _event(
        source="telegram",
        kind="chat_message",
        payload={"text": "hello", "_sender": {"platform": "telegram", "handle": "vedant"}},
    )
    spec = WatchSpec(label="chat", scope="chat", sender="@vedant")

    assert watch_matches(spec, event) is True


def test_urgency_min_is_a_floor() -> None:
    spec = WatchSpec(label="urgent", scope="mail", urgency_min=2)

    assert watch_matches(spec, _mail(urgency=1)) is False
    assert watch_matches(spec, _mail(urgency=2)) is True
    assert watch_matches(spec, _mail(urgency=3)) is True


def test_contains_reads_the_event_words_not_its_json() -> None:
    spec = WatchSpec(label="invoice", scope="mail", contains="invoice")

    assert watch_matches(spec, _mail()) is True
    assert (
        watch_matches(spec, _mail(payload={"subject": "lunch", "snippet": "let's eat at one"}))
        is False
    )


def test_a_description_matches_related_wording() -> None:
    spec = WatchSpec(label="offer", scope="mail", description="offer letter")

    assert watch_matches(spec, _mail(payload={"subject": "Your job offer"})) is True
    assert watch_matches(spec, _mail(payload={"subject": "Lunch tomorrow"})) is False


def test_noise_never_matches_even_when_it_looks_urgent() -> None:
    spec = WatchSpec(label="urgent", scope="mail", urgency_min=2)

    assert watch_matches(spec, _mail(urgency=3, noise=True)) is False


def test_needs_reply_is_an_and_condition() -> None:
    spec = WatchSpec(label="reply", scope="mail", needs_reply=True)

    assert watch_matches(spec, _mail(needs_reply=False)) is False
    assert watch_matches(spec, _mail(needs_reply=True)) is True


def test_scope_can_name_one_source() -> None:
    spec = WatchSpec(label="gmail", scope="gmail")

    assert watch_matches(spec, _mail()) is True
    assert watch_matches(spec, _event(source="telegram", kind="chat_message")) is False


def test_an_empty_watch_is_not_armed_by_the_tool() -> None:
    assert has_criteria(WatchSpec()) is False
    assert has_criteria(WatchSpec(scope="mail")) is True
    assert has_criteria(WatchSpec(urgency_min=2)) is True


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_an_alert_names_the_sender_raw_and_marks_the_owner_second_address() -> None:
    event = _mail(
        urgency=3,
        summary="production is down — fix it now",
        payload={"sender": "Vedant <vedant@other.example>"},
        deadline_at=NOW + timedelta(hours=1),
    )
    watch = Watch(
        id=3,
        spec=WatchSpec(label="urgent mail"),
        status=ACTIVE,
        last_event_id=0,
        last_run_at=None,
        last_notified_at=None,
        created_at=NOW,
    )

    text = render_alert(
        event,
        watch=watch,
        owners={"vedant@other.example"},
        timezone="Asia/Kolkata",
        now=NOW,
    )

    assert "Vedant <vedant@other.example>" in text
    assert "(your other address)" in text
    assert "production is down" in text
    assert "Due 2026-11-03 12:46 IST" in text
    assert 'matched your alert "urgent mail"' in text
    assert "reply, forward it, or remind you later" in text
    # a card is plain words, never a JSON or an internal id
    assert "{" not in text and "E1" not in text


def test_an_alert_marks_an_overdue_deadline() -> None:
    event = _mail(urgency=2, deadline_at=NOW - timedelta(hours=2))

    text = render_alert(event, timezone="UTC", now=NOW)

    assert "Overdue since" in text


def test_a_reminder_card_uses_reminder_wording() -> None:
    event = _mail(urgency=2, summary="the landlord replied")
    watch = Watch(
        id=4,
        spec=WatchSpec(label="landlord", form="reminder"),
        status=ACTIVE,
        last_event_id=0,
        last_run_at=None,
        last_notified_at=None,
        created_at=NOW,
    )

    text = render_alert(event, watch=watch, timezone="UTC", now=NOW)

    assert 'matched your reminder "landlord"' in text
    assert "matched your alert" not in text


def test_an_old_spec_without_a_form_reads_as_an_alert() -> None:
    spec = WatchSpec.from_payload({"label": "x", "scope": "mail"})
    assert spec.form == "alert"


def test_a_reminder_form_is_not_a_different_condition() -> None:
    watch = Watch(
        id=1,
        spec=WatchSpec(label="x", scope="mail", urgency_min=2),
        status=ACTIVE,
        last_event_id=0,
        last_run_at=None,
        last_notified_at=None,
        created_at=NOW,
    )
    same = WatchSpec(label="x", scope="mail", urgency_min=2, form="reminder")

    assert watch.equivalent(same) is True


def test_a_default_urgent_alert_has_no_alert_label() -> None:
    event = _mail(urgency=2, summary="the server is down")

    text = render_alert(event, timezone="UTC", now=NOW)

    assert "Urgent mail from" in text
    assert "matched your alert" not in text
    assert "the server is down" in text


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _store(db) -> WatchStore:
    return WatchStore(db, Cipher.from_b64(generate_key_b64()), AuditLog(db))


@pytest.mark.integration
async def test_a_watch_round_trips_and_its_spec_is_ciphertext(db) -> None:
    watches = _store(db)
    stored = await watches.create(
        spec=WatchSpec(label=SECRET, scope="mail", sender="rahul@example.com", urgency_min=2),
        origin="owner",
    )
    assert stored is not None

    fetched = await watches.get(stored.id)
    assert fetched is not None
    assert fetched.label == SECRET
    assert fetched.spec.sender == "rahul@example.com"
    assert fetched.spec.urgency_min == 2

    blob = await db.fetchval("SELECT spec_enc FROM watches WHERE id = $1", stored.id)
    assert isinstance(blob, bytes)
    assert SECRET.encode() not in blob
    columns = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'watches'"
    )
    assert "label" not in {row["column_name"] for row in columns}


@pytest.mark.integration
async def test_a_non_owner_origin_cannot_arm_a_watch(db) -> None:
    watches = _store(db)

    assert await watches.create(spec=WatchSpec(label="x", scope="mail"), origin="context") is None
    assert await db.fetchval("SELECT COUNT(*) FROM watches") == 0


@pytest.mark.integration
async def test_an_empty_watch_is_refused(db) -> None:
    watches = _store(db)

    assert await watches.create(spec=WatchSpec(), origin="owner") is None


@pytest.mark.integration
async def test_the_same_watch_is_not_armed_twice(db) -> None:
    watches = _store(db)
    spec = WatchSpec(label="urgent", scope="mail", urgency_min=2)

    first = await watches.create(spec=spec, origin="owner")
    second = await watches.create(spec=spec, origin="owner")

    assert first is not None and second is not None and first.id == second.id
    assert await db.fetchval("SELECT COUNT(*) FROM watches") == 1


@pytest.mark.integration
async def test_updating_an_alert_replaces_its_condition(db) -> None:
    watches = _store(db)
    stored = await watches.create(
        spec=WatchSpec(label="x", scope="mail", sender="first@example.com"),
        origin="owner",
    )
    assert stored is not None

    updated = await watches.update(
        stored.id,
        spec=WatchSpec(label="x", scope="mail", sender="second@example.com"),
        origin="owner",
    )

    assert updated is not None and updated.spec.sender == "second@example.com"
    fetched = await watches.get(stored.id)
    assert fetched is not None and fetched.spec.sender == "second@example.com"


@pytest.mark.integration
async def test_pausing_hides_a_watch_from_the_active_set(db) -> None:
    watches = _store(db)
    stored = await watches.create(spec=WatchSpec(label="x", scope="mail"), origin="owner")
    assert stored is not None
    assert [watch.id for watch in await watches.list_active()] == [stored.id]

    paused = await watches.set_enabled(stored.id, False)
    assert paused is not None and paused.status == PAUSED
    assert await watches.list_active() == []

    armed = await watches.set_enabled(stored.id, True)
    assert armed is not None and armed.status == ACTIVE


@pytest.mark.integration
async def test_the_watermark_only_moves_forward(db) -> None:
    watches = _store(db)
    stored = await watches.create(spec=WatchSpec(label="x", scope="mail"), origin="owner")
    assert stored is not None

    await watches.mark_run([stored.id], 10)
    await watches.mark_run([stored.id], 4)

    fetched = await watches.get(stored.id)
    assert fetched is not None and fetched.last_event_id == 10
