"""The gmail read filter, machine-checked: the inbox a bulk read returns
holds only what the user's Gmail list (and the cross-app bucket) allow —
allowlisted senders, mail the user sent an allowlisted person, and
self-addressed mail. Everything else is removed before the agent sees it,
and the result itself says so in the same plain words every turn (so the
model can tell the user their mail is filtered, instead of presenting a
trimmed inbox as the whole truth). The audit row names the count — a
sender address never reaches the log."""

from __future__ import annotations

import json

from fakes import FakeAudit

from aether.config import AppContacts, ContactRule, ContactsConfig
from aether.connectors.mail_allowlist import MailAllowlist


# the live FETCH_EMAILS shape: sender/to arrive as 'Name <addr>' or bare
def _envelope(*messages: dict) -> str:
    return json.dumps({"successful": True, "data": {"messages": list(messages)}})


def _msg(sender: str, to: str = "me@example.com", subject: str = "hello") -> dict:
    return {"sender": sender, "to": to, "subject": subject, "messageId": f"m-{subject}"}


def _filter(contacts: ContactsConfig, audit: FakeAudit | None = None) -> MailAllowlist:
    return MailAllowlist(contacts, audit or FakeAudit())


async def test_mode_off_leaves_the_inbox_untouched() -> None:
    contacts = ContactsConfig(mode="off")  # the default — no filtering
    raw = _envelope(_msg("Newsletter <noreply@digest.example.com>", subject="newsletter"))
    out = await _filter(contacts).filter_result("composio__GMAIL_FETCH_EMAILS", raw)
    assert out == raw


async def test_gmail_section_mode_off_lets_everything_through() -> None:
    # the app's own switch overrides the default
    contacts = ContactsConfig(mode="enforce", gmail=AppContacts(mode="off"))
    raw = _envelope(_msg("Newsletter <noreply@digest.example.com>"))
    assert await _filter(contacts).filter_result("composio__GMAIL_FETCH_EMAILS", raw) == raw


async def test_a_newsletter_from_a_stranger_is_dropped() -> None:
    contacts = ContactsConfig(mode="enforce")
    audit = FakeAudit()
    out = json.loads(
        await _filter(contacts, audit).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(_msg("Newsletter <noreply@digest.example.com>", subject="tips")),
        )
    )
    assert out["data"]["messages"] == []
    # one audit row, the count only — never a sender address
    assert len(audit.entries) == 1
    assert audit.entries[0]["decision"] == "filtered"
    assert audit.entries[0]["params"] == {"messages_kept": 0, "messages_dropped": 1}
    # no address anywhere in the row — outcome and params carry the count only
    assert "noreply@digest.example.com" not in json.dumps(audit.entries[0], default=str)


async def test_an_allowlisted_sender_stays() -> None:
    contacts = ContactsConfig(
        mode="enforce",
        gmail=AppContacts(allowlist=[ContactRule(platform="gmail", handle="abc123@xyz.com")]),
    )
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(
                _msg("abc123@xyz.com", subject="from the list"),
                _msg("Newsletter <noreply@digest.example.com>", subject="noise"),
            ),
        )
    )
    assert [m["subject"] for m in out["data"]["messages"]] == ["from the list"]


async def test_an_any_platform_rule_stays() -> None:
    # the cross-app bucket counts on gmail too
    contacts = ContactsConfig(mode="enforce", allowlist=[ContactRule(handle="dhak3212@hdh.com")])
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS", _envelope(_msg("dhak3212@hdh.com"))
        )
    )
    assert len(out["data"]["messages"]) == 1


async def test_self_to_self_mail_stays() -> None:
    # the user's rule: abc@xyz.com to abc@xyz.com is visible — visible
    # without the filter ever knowing the account's own address
    contacts = ContactsConfig(mode="enforce")
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(_msg("abc@xyz.com", to="abc@xyz.com", subject="to myself")),
        )
    )
    assert [m["subject"] for m in out["data"]["messages"]] == ["to myself"]


async def test_mail_sent_to_an_allowlisted_person_stays() -> None:
    contacts = ContactsConfig(
        mode="enforce", gmail=AppContacts(allowlist=[ContactRule(handle="def@efg.com")])
    )
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(
                # abc@xyz.com to def@efg.com — the recipient is on the list
                _msg("abc@xyz.com", to="def@efg.com", subject="to a friend"),
                # abc@xyz.com to a stranger — dropped
                _msg("abc@xyz.com", to="stranger@elsewhere.net", subject="to a stranger"),
            ),
        )
    )
    assert [m["subject"] for m in out["data"]["messages"]] == ["to a friend"]


async def test_display_name_and_bare_addresses_both_parse() -> None:
    contacts = ContactsConfig(
        mode="enforce", gmail=AppContacts(allowlist=[ContactRule(handle="friend@example.com")])
    )
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(
                _msg("Friendo <friend@example.com>", subject="angled"),
                _msg("friend@example.com", subject="bare"),
                _msg("Nobody <stranger@somewhere.io>", subject="dropped"),
            ),
        )
    )
    assert [m["subject"] for m in out["data"]["messages"]] == ["angled", "bare"]


async def test_multi_recipient_to_matches_one_allowlisted() -> None:
    contacts = ContactsConfig(
        mode="enforce", gmail=AppContacts(allowlist=[ContactRule(handle="def@efg.com")])
    )
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(_msg("me@example.com", to="def@efg.com, other@elsewhere.net")),
        )
    )
    assert len(out["data"]["messages"]) == 1


async def test_no_messages_dropped_means_no_audit_row() -> None:
    contacts = ContactsConfig(mode="off")
    audit = FakeAudit()
    await _filter(contacts, audit).filter_result(
        "composio__GMAIL_FETCH_EMAILS", _envelope(_msg("anyone@example.com"))
    )
    assert audit.entries == []


async def test_non_gmail_names_and_unreadable_results_pass_through() -> None:
    contacts = ContactsConfig(mode="enforce")
    f = _filter(contacts)
    assert await f.filter_result("composio__NOTION_SEARCH", "whatever") == "whatever"
    assert await f.filter_result("composio__GMAIL_WHO_AM_I", "not json") == "not json"
    # an envelope without a messages array — nothing to walk, untouched
    who = json.dumps({"successful": True, "data": {"email": "me@example.com"}})
    assert await f.filter_result("composio__GMAIL_WHO_AM_I", who) == who


async def test_unknown_message_shapes_are_never_destroyed() -> None:
    contacts = ContactsConfig(mode="enforce")
    raw = _envelope({"unexpected": ["shape"]})
    assert await _filter(contacts).filter_result("composio__GMAIL_FETCH_EMAILS", raw) == raw


async def test_filtered_results_say_so_in_the_same_words_every_turn() -> None:
    # the note rides the envelope itself, so the model can tell the user
    # their mail is filtered — identical wording whether mail was dropped
    # or every sender happened to be on the list
    contacts = ContactsConfig(
        mode="enforce", gmail=AppContacts(allowlist=[ContactRule(handle="friend@example.com")])
    )
    f = _filter(contacts)

    with_drops = json.loads(
        await f.filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(
                _msg("friend@example.com", subject="kept"),
                _msg("stranger@elsewhere.net", subject="dropped"),
            ),
        )
    )
    assert with_drops["data"]["allowlist_note"] == (
        "these results are filtered by the Gmail allowlist — 1 message not on it was left out"
    )

    all_kept = json.loads(
        await f.filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(_msg("friend@example.com", subject="kept")),
        )
    )
    assert all_kept["data"]["allowlist_note"] == (
        "these results are filtered by the Gmail allowlist"
    )


async def test_plural_drops_read_plurally() -> None:
    contacts = ContactsConfig(mode="enforce")
    out = json.loads(
        await _filter(contacts).filter_result(
            "composio__GMAIL_FETCH_EMAILS",
            _envelope(
                _msg("a@b.c", subject="one"),
                _msg("x@y.z", subject="two"),
                _msg("w@v.u", subject="three"),
            ),
        )
    )
    assert out["data"]["allowlist_note"].endswith(" — 3 messages not on it were left out")


async def test_an_off_allowlist_never_claims_its_results_are_filtered() -> None:
    # claiming a filtering that isn't happening would be its own kind of
    # lie — the inbox really is the whole truth when the switch is off
    contacts = ContactsConfig(mode="enforce", gmail=AppContacts(mode="off"))
    out = await _filter(contacts).filter_result(
        "composio__GMAIL_FETCH_EMAILS", _envelope(_msg("stranger@elsewhere.net"))
    )
    assert "allowlist_note" not in out


async def test_a_from_field_is_judged_like_sender() -> None:
    contacts = ContactsConfig(
        mode="enforce",
        gmail=AppContacts(allowlist=[ContactRule(platform="gmail", handle="friend@example.com")]),
    )
    raw = json.dumps(
        {
            "successful": True,
            "data": {
                "messages": [
                    {"from": "Friend <friend@example.com>", "subject": "kept"},
                    {"from": "stranger@elsewhere.net", "subject": "dropped"},
                ]
            },
        }
    )
    out = json.loads(await _filter(contacts).filter_result("gmail__list_messages", raw))
    assert [m["subject"] for m in out["data"]["messages"]] == ["kept"]


async def test_the_direct_gmail_tool_rides_the_same_filter() -> None:
    contacts = ContactsConfig(
        mode="enforce",
        gmail=AppContacts(allowlist=[ContactRule(platform="gmail", handle="abc123@xyz.com")]),
    )
    out = json.loads(
        await _filter(contacts).filter_result(
            "gmail__list_messages",
            _envelope(
                _msg("abc123@xyz.com", subject="from the list"),
                _msg("stranger@elsewhere.net", subject="noise"),
            ),
        )
    )
    assert [m["subject"] for m in out["data"]["messages"]] == ["from the list"]
    assert out["data"]["allowlist_note"].endswith(" — 1 message not on it was left out")
