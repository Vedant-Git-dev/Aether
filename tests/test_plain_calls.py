"""The backend plain-language vocabulary for tool calls: `describe_call`
and the read-time backfill helpers the API uses for rows that predate the
`plain` field. The panel's contract — no raw tool name, no JSON, ever —
rests on these sentences."""

from __future__ import annotations

from aether.agent.tools import (
    describe_ask,
    describe_call,
    describe_dismissed,
    describe_full,
    describe_outcome,
    plain_outcome,
    with_plain_calls,
    with_plain_event,
)


class TestDescribeCall:
    def test_read_like_mcp_tool_reads_as_a_check(self) -> None:
        assert describe_call("mail__list_messages") == "Checking mail"

    def test_send_words_carry_the_salient_context_never_a_dump(self) -> None:
        # the body rides as a clipped one-line preview, never a recital
        assert (
            describe_call(
                "mail__send_message",
                {"to": "sam@example.com", "subject": "Re: Thursday", "body": "x" * 200},
            )
            == "Sending an email (to sam@example.com, subject 'Re: Thursday', body "
            + "x" * 77
            + "...)"
        )

    def test_the_body_preview_flattens_and_clips(self) -> None:
        # newlines and blank space flatten to one line, and anything past
        # the preview length clips — the card says what the message says
        # without becoming the message
        assert (
            describe_call(
                "mail__send_message",
                {"to": "sam@example.com", "body": "line one\n\nline two " + "y" * 200},
            )
            == f"Sending an email (to sam@example.com, body 'line one line two {'y' * 59}...')"
        )

    def test_composio_recipient_email_reads_as_the_recipient(self) -> None:
        # composio's gmail spells the recipient recipient_email — without
        # the mapping the approval card showed only the subject
        assert describe_call(
            "composio__GMAIL_SEND_EMAIL",
            {"recipient_email": "sam@example.com", "subject": "hi", "body": "quick note"},
        ) == ("Sending an email (to sam@example.com, subject hi, body 'quick note')")

    def test_composio_reads_run_the_verb_check_on_the_action(self) -> None:
        # regression: the read-like check must see "list_messages", not
        # "gmail_list_messages" — or every composio read reads as a send
        assert describe_call("composio__GMAIL_LIST_MESSAGES") == "Checking gmail"

    def test_composio_who_am_i_idiom(self) -> None:
        assert describe_call("composio__GMAIL_WHO_AM_I") == "Checking the connected gmail account"

    def test_chat_ref_reads_as_the_recipient(self) -> None:
        assert describe_call("telegram__send_message", {"chat_ref": "@sam"}) == (
            "Sending a message (to @sam)"
        )

    def test_a_raw_id_is_never_salient_context(self) -> None:
        # "chat 8977329495" and "channel C03AB" tell a user nothing — a
        # numeric or token id is an internal reference, so the preview
        # carries no context at all rather than an opaque number
        assert (
            describe_call("telegram__send_message", {"chat_id": 8977329495, "text": "hi"})
            == "Sending a message"
        )
        assert (
            describe_call("slack__send_message", {"channel_id": "C03ABCDEF", "text": "hi"})
            == "Sending a message"
        )

    def test_native_tool_words(self) -> None:
        assert describe_call("memory_search", {"query": "sam"}) == "Searching my memory (query sam)"

    def test_unknown_app_falls_back_without_the_raw_name(self) -> None:
        text = describe_call("composio__HORIZON_SEND_PING")
        assert text == "An action in horizon"
        assert "HORIZON" not in text and "__" not in text

    def test_direct_gmail_actions_read_as_their_own_act(self) -> None:
        # the app-level "gmail" words are a send; the read and draft tools
        # must not inherit that
        assert describe_call("gmail__list_messages") == "Checking the mail"
        assert describe_call("gmail__get_message") == "Reading the email"
        assert describe_call("gmail__send_email", {"to": "sam@example.com"}) == (
            "Sending the email (to sam@example.com)"
        )
        assert describe_call("gmail__create_draft") == "Drafting the email"
        assert describe_call("gmail__send_draft") == "Sending the draft"
        assert describe_call("gmail__get_profile") == "Checking the connected gmail account"

    def test_direct_calendar_actions_read_as_their_own_act(self) -> None:
        assert describe_call("calendar__list_events") == "Checking the calendar"
        assert describe_call("calendar__create_event") == "Creating the event"
        assert describe_call("calendar__update_event") == "Updating the event"
        assert describe_call("calendar__delete_event") == "Deleting the event"
        assert describe_call("calendar__freebusy") == "Checking availability"


class TestDescribeOutcome:
    """The fallback chat line when an approved call succeeds with no
    reasoning provider wired — the result stated plainly, with no
    "Done — tool executed" ceremony. With a provider, the model's own
    reply is what the user hears."""

    def test_success_is_the_plain_done_sentence(self) -> None:
        assert (
            describe_outcome("mail__send_message", {"to": "sam@example.com"})
            == "Sent the email (to sam@example.com)."
        )

    def test_no_context_variant(self) -> None:
        assert describe_outcome("mail__send_message") == "Sent the email."


class TestDescribeDismissed:
    """The fallback chat acknowledgment when a denial arrives with no
    reasoning provider wired — a plain okay. With a provider, the
    model's own reply acknowledges the denial instead."""

    def test_denial_acknowledgment(self) -> None:
        assert (
            describe_dismissed("mail__send_message", {"to": "sam@example.com"})
            == "Okay — not sending an email (to sam@example.com)."
        )

    def test_no_context_variant(self) -> None:
        assert describe_dismissed("mail__send_message") == "Okay — not sending an email."


class TestPlainOutcome:
    def test_done_is_the_past_tense_sentence(self) -> None:
        # a completed act must read as finished — the doing form
        # ("Sending an email…") on a done event is what a later turn
        # re-drafts as something still left to do
        assert plain_outcome("mail__list_messages", {}, "action_done") == "Checked mail"
        assert (
            plain_outcome("mail__send_message", {"to": "sam@example.com"}, "action_done")
            == "Sent the email (to sam@example.com)"
        )

    def test_failed_is_framed(self) -> None:
        assert plain_outcome("mail__send_message", {"to": "sam@example.com"}, "action_failed") == (
            "Failed — sending an email (to sam@example.com)"
        )

    def test_denied_reads_as_a_denial(self) -> None:
        assert plain_outcome("mail__send_message", {}, "action_denied") == (
            "Not run — you denied sending an email"
        )


class TestWithPlainCalls:
    def test_fills_missing_plain(self) -> None:
        out = with_plain_calls({"calls": [{"name": "mail__list_messages", "params": {}}]})
        assert out["calls"][0]["plain"] == "Checking mail"

    def test_never_overwrites_an_existing_plain(self) -> None:
        out = with_plain_calls(
            {
                "calls": [
                    {"name": "mail__list_messages", "params": {}, "plain": "Checking mail for Sam"}
                ]
            }
        )
        assert out["calls"][0]["plain"] == "Checking mail for Sam"

    def test_does_not_mutate_the_stored_payload(self) -> None:
        payload = {"calls": [{"name": "mail__list_messages", "params": {}}]}
        with_plain_calls(payload)
        assert "plain" not in payload["calls"][0]

    def test_passes_through_payloads_without_calls(self) -> None:
        assert with_plain_calls({"result": "ok"}) == {"result": "ok"}
        assert with_plain_calls(None) is None


class TestWithPlainEvent:
    def test_fills_a_legacy_agent_outcome(self) -> None:
        out = with_plain_event(
            {"tool": "composio__GMAIL_WHO_AM_I", "params": {}}, kind="action_done"
        )
        assert out["plain"] == "Checked the connected gmail account"

    def test_never_overwrites_an_existing_plain(self) -> None:
        out = with_plain_event(
            {"tool": "mail__list_messages", "params": {}, "plain": "Checking mail for Sam"},
            kind="action_done",
        )
        assert out["plain"] == "Checking mail for Sam"

    def test_leaves_non_agent_payloads_alone(self) -> None:
        assert with_plain_event({"text": "hi"}, kind="chat_message") == {"text": "hi"}


class TestDescribeAsk:
    """The approval question a chat surface carries: the action as a
    question, then the request's own fields in their own order — never
    the tool name, never a JSON dump, never the gate's reason. Long text
    previews its leading lines; "read the full text" is the same layout
    with everything complete."""

    def test_the_question_then_the_fields_in_the_request_own_order(self) -> None:
        assert (
            describe_ask(
                "mail__send_message",
                {"to": "sam@example.com", "subject": "hi", "body": "quick note"},
            )
            == "Send the email?\n\nTo: sam@example.com\nSubject: hi\nBody: quick note"
        )

    def test_an_ask_with_no_fields_says_so(self) -> None:
        assert describe_ask("mail__send_message") == "Send the email?\n\n(empty)"

    def test_a_long_value_previews_leading_lines_with_no_ellipsis(self) -> None:
        # the question carries the request's opening, not its end — and
        # never a "…" that could pass for content; the read button says
        # there is more, so the preview never has to
        body = "\n".join(f"line {i:02d} " + "y" * 30 for i in range(20))
        text = describe_ask("mail__send_message", {"to": "a@b.c", "body": body})
        assert "Body:" in text
        assert "line 00" in text
        assert "line 05" in text
        assert "line 06" not in text
        assert "…" not in text and "..." not in text

    def test_a_single_line_past_the_budget_cuts_at_a_word(self) -> None:
        # no line boundary to stop at — the cut lands at one, so the
        # preview still reads as words
        body = "one two " + "x" * 400
        text = describe_ask("mail__send_message", {"to": "a@b.c", "body": body})
        assert "Body: one two" in text
        assert "xxx" not in text

    def test_the_full_text_is_the_same_ask_with_everything_complete(self) -> None:
        # one composer, one layout: reading the full text only adds the
        # rest of the content, never a different presentation
        params = {"to": "a@b.c", "body": "\n".join(f"line {i:02d}" for i in range(20))}
        full = describe_full("mail__send_message", params)
        assert full.startswith(describe_ask("mail__send_message", params))
        assert "line 19" in full

    def test_internal_references_stay_pointers(self) -> None:
        text = describe_ask(
            "composio__GMAIL_REPLY_TO_THREAD",
            {"thread_id": "19c2e6fe0b2f1a34", "message": "Sounds good."},
        )
        assert "Thread id: (kept in the decision record)" in text
        assert "19c2e6fe0b2f1a34" not in text


class TestDescribeFull:
    """The whole ask behind the chat surfaces' "read the full text" button:
    the same title, labels and order as the approval question, with every
    field complete — the exact text a decision applies to. An internal
    reference points at the decision record's exact copy rather than
    printing its token."""

    def test_every_field_in_the_request_own_order(self) -> None:
        text = describe_full(
            "mail__send_message",
            {"to": "sam@example.com", "subject": "hi", "body": "quick note"},
        )
        assert text == "Send the email?\n\nTo: sam@example.com\nSubject: hi\nBody: quick note"

    def test_the_gate_reason_never_reaches_the_text(self) -> None:
        # the note is wiring — why the request parked is not part of what
        # a decision applies to, so it never renders
        text = describe_full("mail__send_message", {"to": "a@b.c"}, note="hard to undo")
        assert text == "Send the email?\n\nTo: a@b.c"
        assert "needs approval" not in text
        assert "hard to undo" not in text

    def test_nothing_clips(self) -> None:
        # the preview cuts a long body to a hint; the full text carries it
        # whole, so nothing is out of reach of a decision
        long_body = "x" * 5000
        assert long_body in describe_full("mail__send_message", {"body": long_body})

    def test_a_multiline_body_keeps_its_own_lines(self) -> None:
        # a body or a patch must copy clean — its lines stay its lines
        text = describe_full("mail__send_message", {"body": "line one\nline two"})
        assert "Body:\nline one\nline two" in text

    def test_lists_number_their_items_and_dicts_label_their_fields(self) -> None:
        text = describe_full(
            "calendar__create_event",
            {"attendees": [{"name": "Sam Rivera", "email": "sam@example.com"}]},
        )
        assert "Attendees:" in text
        assert "1. Name: Sam Rivera" in text
        assert "   Email: sam@example.com" in text  # aligned under its item

    def test_empties_say_so(self) -> None:
        text = describe_full(
            "mail__send_message", {"to": "", "cc": None, "attachments": [], "headers": {}}
        )
        assert "To: (empty)" in text
        assert "Cc: (empty)" in text
        assert "Attachments: (empty)" in text
        assert "Headers: (empty)" in text

    def test_an_ask_with_no_fields_says_so(self) -> None:
        assert describe_full("mail__send_message") == "Send the email?\n\n(empty)"

    def test_a_date_shaped_value_reads_as_a_date(self) -> None:
        # the same words the panel's card uses — never the raw ISO string
        text = describe_full("calendar__create_event", {"start": "2026-10-28T09:00:00"})
        assert "Start: Oct 28, 2026, 9:00 AM" in text
        assert "2026-10-28" not in text

    def test_an_offset_the_value_carried_is_kept(self) -> None:
        text = describe_full("calendar__create_event", {"start": "2026-10-28T09:00:00+05:30"})
        assert "Oct 28, 2026, 9:00 AM (UTC+05:30)" in text

    def test_a_value_the_pattern_does_not_match_stays_untouched(self) -> None:
        # "2026-10-28" is a date to a human but not the datetime shape the
        # renderer translates — it must arrive exactly as the tool sent it
        text = describe_full("calendar__create_event", {"start": "2026-10-28"})
        assert "Start: 2026-10-28" in text

    def test_flags_read_as_yes_and_no(self) -> None:
        text = describe_full("set_config", {"dry_run": True, "notify": False})
        assert "Dry run: yes" in text
        assert "Notify: no" in text

    def test_internal_underscore_fields_stay_out(self) -> None:
        # plumbing keys are not part of the ask a human decides on — the
        # panel's card skips them, and so does the full text
        text = describe_full("mail__send_message", {"_trace": "abc", "to": "a@b.c"})
        assert "_trace" not in text and "Trace" not in text
        assert "To: a@b.c" in text

    def test_no_json_anywhere(self) -> None:
        # the plain-language contract holds for the full text too: shapes
        # render as labelled lines, never as braces and quotes
        text = describe_full("mail__send_message", {"to": "a@b.c", "flags": {"dry_run": True}})
        assert "{" not in text and "}" not in text
        assert "Dry run: yes" in text

    def test_an_internal_reference_never_prints_its_token(self) -> None:
        # the real gmail reply ask: the thread is an opaque token naming a
        # conversation — the full text says where the exact value lives,
        # and the words a decision is made from stay words
        text = describe_full(
            "composio__GMAIL_REPLY_TO_THREAD",
            {"thread_id": "19c2e6fe0b2f1a34", "message": "Sounds good — Thursday works."},
        )
        assert "Thread id: (kept in the decision record)" in text
        assert "19c2e6fe0b2f1a34" not in text
        assert "Message: Sounds good — Thursday works." in text

    def test_numeric_ids_and_a_bare_id_read_the_same(self) -> None:
        # an Aether id is as internal as a connected app's — the treatment
        # comes from the key, never from what the token looks like
        text = describe_full("share_media", {"media_id": 42, "to": "@sam"})
        assert "Media id: (kept in the decision record)" in text
        assert "42" not in text
        text = describe_full("calendar__get_event", {"id": "evt_19", "title": "Dentist"})
        assert "Id: (kept in the decision record)" in text
        assert "evt_19" not in text
        assert "Title: Dentist" in text

    def test_a_reference_inside_a_nested_structure_is_covered_too(self) -> None:
        text = describe_full(
            "mail__send_message",
            {"reactions": [{"user_id": "u_77", "emoji": "wave"}]},
        )
        assert "User id: (kept in the decision record)" in text
        assert "u_77" not in text
        assert "Emoji: wave" in text

    def test_an_empty_reference_is_still_just_empty(self) -> None:
        # no token, nothing kept — a blank id reads like any other blank
        text = describe_full("composio__GMAIL_REPLY_TO_THREAD", {"thread_id": None})
        assert "Thread id: (empty)" in text
