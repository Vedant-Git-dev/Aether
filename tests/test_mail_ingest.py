"""Mail intake: one event per message, raw headers kept.

The Gmail poll used to land as a single JSON blob truncated to 4000
characters — every message of the poll merged into one row and the tail
cut off. These tests pin the replacement: a mail is one event, it carries
the raw From/To/Cc the provider returned, its own timestamp comes from the
Date header, and the allowlist still judges the envelope before anything
is stored.

Dedup on the provider's message id needs a real store, so those cases live
behind the integration marker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_agent_loop import FakeProviderRuntime, LoopKit

from aether.authz.audit import AuditLog
from aether.config import ContactRule, ContactsConfig
from aether.connectors.mail_allowlist import MailAllowlist
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.events import EventStore
from aether.memory.mail import (
    MAIL_KIND,
    MAIL_SOURCE,
    Address,
    parse_addresses,
    parse_sent_at,
    split_mail,
)
from aether.providers.types import PollResult

SENT = datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def _message(message_id: str = "m1", **overrides) -> dict:
    message = {
        "id": message_id,
        "thread_id": f"t-{message_id}",
        "sender": "Rahul Sharma <rahul@example.com>",
        "to": "me@example.com",
        "cc": "Priya <priya@example.com>",
        "subject": "Invoice for October",
        "date": "Tue, 03 Nov 2026 11:46:00 +0530",
        "snippet": "please find attached",
        "body": "Please find the invoice attached.",
        "label_ids": ["INBOX", "UNREAD"],
        "unread": True,
    }
    message.update(overrides)
    return message


def _envelope(*messages: dict) -> dict:
    return {"successful": True, "data": {"messages": list(messages)}}


# ---------------------------------------------------------------------------
# address and time parsing
# ---------------------------------------------------------------------------


def test_parse_addresses_reads_names_and_addresses() -> None:
    assert parse_addresses("Rahul Sharma <rahul@example.com>") == (
        Address(name="Rahul Sharma", address="rahul@example.com"),
    )


def test_parse_addresses_handles_a_list_a_bare_address_and_a_quoted_comma() -> None:
    parsed = parse_addresses('"Sharma, Rahul" <rahul@example.com>, priya@example.com')
    assert parsed == (
        Address(name="Sharma, Rahul", address="rahul@example.com"),
        Address(name="", address="priya@example.com"),
    )


def test_parse_addresses_on_nothing_is_empty() -> None:
    assert parse_addresses("") == ()
    assert parse_addresses(None) == ()


def test_parse_sent_at_reads_the_header_into_utc() -> None:
    when = parse_sent_at("Tue, 03 Nov 2026 11:46:00 +0530", SENT)
    assert when == datetime(2026, 11, 3, 6, 16, tzinfo=UTC)


def test_parse_sent_at_treats_a_naive_header_as_utc() -> None:
    # a Date with no offset is UTC per RFC 5322, never the reader's local time
    assert parse_sent_at("Tue, 03 Nov 2026 06:16:00", SENT) == SENT


def test_parse_sent_at_falls_back_on_a_missing_or_broken_date() -> None:
    assert parse_sent_at("", SENT) == SENT
    assert parse_sent_at("today", SENT) == SENT
    assert parse_sent_at(None, SENT) == SENT


# ---------------------------------------------------------------------------
# the split
# ---------------------------------------------------------------------------


def test_split_mail_makes_one_event_per_message() -> None:
    events = split_mail(_envelope(_message("m1"), _message("m2"), _message("m3")))
    assert [event.message_id for event in events] == ["m1", "m2", "m3"]
    assert all(event.occurred_at == SENT for event in events)
    assert events[0].sender == "rahul@example.com"
    assert events[0].dedup_key == "gmail:m1"


def test_split_mail_keeps_the_raw_headers_and_the_body() -> None:
    (event,) = split_mail(_envelope(_message()))
    assert event.payload["sender"] == "Rahul Sharma <rahul@example.com>"
    assert event.payload["to"] == "me@example.com"
    assert event.payload["cc"] == "Priya <priya@example.com>"
    assert event.payload["subject"] == "Invoice for October"
    assert event.payload["body"] == "Please find the invoice attached."
    assert event.payload["label_ids"] == ["INBOX", "UNREAD"]
    assert event.payload["unread"] is True


def test_split_mail_keeps_the_provider_field_names_the_allowlist_judges() -> None:
    # the Gmail allowlist reads `sender`/`to` off the envelope it is given;
    # renaming them would quietly disarm the filter
    (event,) = split_mail(_envelope(_message()))
    assert set(event.payload) >= {"sender", "to", "cc"}


def test_split_mail_skips_a_message_with_no_id() -> None:
    events = split_mail(_envelope(_message(), {"sender": "x@y.com"}))
    assert [event.message_id for event in events] == ["m1"]


def test_split_mail_ignores_a_shape_it_does_not_model() -> None:
    assert split_mail({"successful": True}) == []
    assert split_mail({"result": "text"}) == []
    assert split_mail(None) == []
    assert split_mail(_envelope()) == []


def test_split_mail_falls_back_to_now_for_an_unreadable_date() -> None:
    (event,) = split_mail(_envelope(_message(date="not a date")), now=SENT)
    assert event.occurred_at == SENT


# ---------------------------------------------------------------------------
# the loop's poll path
# ---------------------------------------------------------------------------


class FakeFilterAudit:
    """The MailAllowlist only needs an audit sink for its drop row; the
    loop's own audit is `kit.audit`, so this one just swallows it."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def append(self, **entry) -> int:
        self.entries.append(entry)
        return len(self.entries)


def _contacts(*rules: dict) -> ContactsConfig:
    return ContactsConfig(mode="enforce", allowlist=[ContactRule(**r) for r in rules])


def _poll(messages: list[dict]) -> PollResult:
    return PollResult(
        app="gmail",
        tool="gmail__new_mail",
        payload={"successful": True, "data": {"messages": messages}},
    )


async def _run_poll(*, messages: list[dict], contacts: ContactsConfig | None = None) -> LoopKit:
    """A loop with the real MailAllowlist over a scripted poll."""
    runtime = FakeProviderRuntime(polls=[_poll(messages)])
    kit = LoopKit(
        None,
        provider_runtime=runtime,
        mail_filter=MailAllowlist(contacts or ContactsConfig(), FakeFilterAudit()),
    )
    await kit.loop._run_due_provider_polls()
    return kit


async def test_a_poll_becomes_one_event_per_message() -> None:
    kit = await _run_poll(messages=[_message("m1"), _message("m2"), _message("m3")])

    assert [call["kind"] for call in kit.events.ingest_calls] == [MAIL_KIND] * 3
    assert [call["source"] for call in kit.events.ingest_calls] == [MAIL_SOURCE] * 3
    assert [call["dedup_key"] for call in kit.events.ingest_calls] == [
        "gmail:m1",
        "gmail:m2",
        "gmail:m3",
    ]
    assert [call["payload"]["message_id"] for call in kit.events.ingest_calls] == ["m1", "m2", "m3"]
    # no blob: the old shape stored one row holding every message, truncated
    assert all("result" not in call["payload"] for call in kit.events.ingest_calls)


async def test_a_polled_message_carries_its_own_timestamp() -> None:
    kit = await _run_poll(messages=[_message()])
    assert kit.events.ingest_calls[0]["occurred_at"] == SENT


async def test_a_polled_message_records_its_thread_and_sender() -> None:
    kit = await _run_poll(messages=[_message()])
    meta = kit.events.ingest_calls[0]["meta"]
    assert meta["thread_id"] == "t-m1"
    assert meta["message_id"] == "m1"
    assert meta["from_address"] == "rahul@example.com"


async def test_no_sender_rides_a_mail_ingest() -> None:
    # the mail allowlist — not the contact allowlist — judges mail, and
    # re-judging here would drop mail the user *sent* to an allowlisted
    # person, whose sender is the user themselves
    kit = await _run_poll(messages=[_message()])
    assert kit.events.ingest_calls[0]["sender"] is None


async def test_the_allowlist_drops_a_sender_before_the_split() -> None:
    kit = await _run_poll(
        messages=[
            _message("m1"),
            _message("m2", sender="Stranger <stranger@example.com>"),
        ],
        contacts=_contacts({"platform": "email", "handle": "rahul@example.com"}),
    )

    assert [call["payload"]["message_id"] for call in kit.events.ingest_calls] == ["m1"]


async def test_a_poll_with_nothing_splittable_keeps_the_blob_shape() -> None:
    # a non-mail provider poll still lands as one event, exactly as before
    runtime = FakeProviderRuntime(
        polls=[PollResult(app="calendar", tool="calendar__new_events", payload={"events": [1]})]
    )
    kit = LoopKit(None, provider_runtime=runtime)

    await kit.loop._run_due_provider_polls()

    assert kit.events.ingested == [
        {
            "source": "calendar",
            "kind": "poll:new_events",
            "payload": {"result": '{"events": [1]}'},
            "sender": None,
        }
    ]


# ---------------------------------------------------------------------------
# chunking a mail (plan §4.5)
# ---------------------------------------------------------------------------


def test_a_mail_chunk_carries_a_header_naming_it() -> None:
    from aether.memory.vectors import _chunk_text

    parts = _chunk_text(
        MAIL_SOURCE,
        MAIL_KIND,
        {
            "subject": "Invoice for October",
            "sender": "Rahul Sharma <rahul@example.com>",
            "date": "Tue, 03 Nov 2026 11:46:00 +0530",
            "body": "Please find the invoice attached.",
        },
        SENT,
    )

    assert len(parts) == 1
    text = parts[0][1]
    assert text.startswith("[Mail: Invoice for October | from Rahul Sharma <rahul@example.com>")
    assert "2026-11-03 06:16 UTC" in text
    assert text.endswith("Please find the invoice attached.")


def test_a_mails_quoted_history_is_not_indexed() -> None:
    from aether.memory.vectors import _chunk_text

    parts = _chunk_text(
        MAIL_SOURCE,
        MAIL_KIND,
        {
            "subject": "Re: Invoice",
            "sender": "Rahul <rahul@example.com>",
            "body": (
                "yes please\n\n"
                "On Tue, Nov 3 2026 at 11:46 Rahul wrote:\n"
                "> can you send it\n> thanks"
            ),
        },
        SENT,
    )

    text = parts[0][1]
    assert "yes please" in text
    # each reply would otherwise retrieve as every earlier message in the thread
    assert "can you send it" not in text


def test_a_mail_with_no_body_still_indexes_its_snippet() -> None:
    from aether.memory.vectors import _chunk_text

    parts = _chunk_text(
        MAIL_SOURCE, MAIL_KIND, {"subject": "Hi", "snippet": "the short preview"}, SENT
    )

    assert len(parts) == 1
    assert "the short preview" in parts[0][1]


def test_a_bare_mail_with_nothing_to_read_indexes_nothing() -> None:
    from aether.memory.vectors import _chunk_text

    assert _chunk_text(MAIL_SOURCE, MAIL_KIND, {"subject": "", "body": ""}, SENT) == []


def test_a_chunk_header_leaves_out_what_it_does_not_have() -> None:
    from aether.memory.vectors import chunk_header

    assert chunk_header(name="Doc: OSY.pdf", location="slide 3") == "[Doc: OSY.pdf | slide 3]"
    assert chunk_header() == ""
    assert chunk_header(sender="Ada <ada@x.com>").startswith("[from Ada")


# ---------------------------------------------------------------------------
# dedup on the provider's message id (real store)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_same_message_id_is_stored_once(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig())

    first = await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload={"message_id": "m1", "subject": "Hi"},
        dedup_key="gmail:m1",
    )
    # the same message refetched with a changed body is still the same mail
    second = await events.ingest(
        source=MAIL_SOURCE,
        kind=MAIL_KIND,
        payload={"message_id": "m1", "subject": "Hi", "body": "now with a body"},
        dedup_key="gmail:m1",
    )
    assert (first.stored, first.reason) == (True, "new")
    assert (second.stored, second.reason) == (False, "duplicate")
    assert await db.fetchval("SELECT COUNT(*) FROM events") == 1


@pytest.mark.integration
async def test_two_messages_are_two_events(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig())

    for message_id in ("m1", "m2"):
        await events.ingest(
            source=MAIL_SOURCE,
            kind=MAIL_KIND,
            payload={"message_id": message_id, "subject": "same subject"},
            dedup_key=f"gmail:{message_id}",
        )
    # identical bodies, different messages — the provider id is what counts
    assert await db.fetchval("SELECT COUNT(*) FROM events") == 2


@pytest.mark.integration
async def test_payload_dedup_still_applies_without_a_dedup_key(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    events = EventStore(db, cipher, AuditLog(db), ContactsConfig())

    first = await events.ingest(source="mail", kind="message", payload={"text": "hello"})
    second = await events.ingest(source="mail", kind="message", payload={"text": "hello"})
    assert (first.stored, second.stored) == (True, False)
    assert await db.fetchval("SELECT COUNT(*) FROM events") == 1
