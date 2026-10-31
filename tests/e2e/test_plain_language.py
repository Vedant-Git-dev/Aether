"""The plain-language contract, machine-checked: no raw tool names
(x__y), no raw rule ids (builtin:, user:), no raw event kinds
(poll:unread), no raw ISO timestamps, no JSON blobs and no internal
scoring words ever reach visible text — on any page, including every
expanded trace detail. The dev fixture deliberately carries the
backend's real raw shapes (the _event_line log format, UPPER_SNAKE
composio actions, "approval #N — <tool>" labels) so a leak here fails
loudly instead of shipping.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.e2e

PAGES = [
    "activity",
    "attention",
    "tasks",
    "memory",
    "audit",
    "traces",
    "policy",
    "apps",
    "contacts",
    "settings",
]

RAW_PATTERNS = [
    # composio names are UPPER_SNAKE after the __, so the suffix can't be lowercase-only
    (re.compile(r"\b[a-z][a-z0-9]*__[A-Za-z0-9_]+\b"), "raw tool name"),
    (re.compile(r"\b(builtin|default|user|routine|config):[a-z0-9_-]+", re.I), "raw rule id"),
    (re.compile(r"\bpoll:[a-z_]+\b"), "raw event kind"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"), "raw ISO timestamp"),
    (re.compile(r"\{[^{}]*\"[a-z_]+\"[^{}]*:"), "JSON blob"),
    (re.compile(r"\bsalience\b", re.I), "internal scoring word"),
    # the fixture's media rows carry real MIME strings and raw media ids —
    # the renderer must speak plain words, never either
    (re.compile(r"\b(image|video|audio|application)/[a-z0-9.+-]+\b", re.I), "raw MIME type"),
    (re.compile(r"\bmedia[ _-]?id\b", re.I), "raw media id"),
]


def _scan(text: str, where: str) -> None:
    for pattern, what in RAW_PATTERNS:
        hits = pattern.findall(text)
        assert not hits, f"{where}: {what} visible — {hits[:3]!r}"


def test_no_raw_tokens_on_any_page(page, live_server, token) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")
    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector('[data-testid="page-title"]')

    for name in PAGES:
        page.goto(f"{live_server}/#/{name}")
        page.wait_for_selector('[data-testid="page-title"]')
        page.wait_for_timeout(700)  # let the pollers land
        _scan(page.locator("body").inner_text(), name)

    # expand every trace so the lazy detail bodies are scanned too
    page.goto(f"{live_server}/#/traces")
    page.wait_for_selector('[data-testid="trace-row"]')
    heads = page.locator('[data-testid="trace-head"]')
    for i in range(heads.count()):
        heads.nth(i).click()
        page.wait_for_timeout(400)
    page.wait_for_selector('[data-testid="trace-row"][data-open="1"] [data-testid="trace-call"]')
    page.wait_for_timeout(400)
    _scan(page.locator("body").inner_text(), "traces (all expanded)")


def test_plain_renderings_against_the_fixture(page, live_server, token) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")

    page.goto(f"{live_server}/#/traces")
    page.wait_for_selector('[data-testid="trace-row"]')
    heads = page.locator('[data-testid="trace-head"]')
    for i in range(heads.count()):
        heads.nth(i).click()
        page.wait_for_timeout(400)
    page.wait_for_selector('[data-testid="trace-row"][data-open="1"] [data-testid="trace-call"]')
    text = page.locator("body").inner_text()

    # the backend's "approval #N — <raw tool>" label reads plainly
    assert "Approval #1 — Sending" in text
    # the model's own `plain` sentence wins where the loop recorded one
    assert "Emailing Sam the Thursday confirmation" in text
    # a call without `plain` (legacy row) is backfilled by the real route
    # from the backend vocabulary — "telegram__send_message" never shows
    assert "Sending a message (to @sam)" in text
    # a composio UPPER_SNAKE action reads as the app, never "composio"
    assert "Sending an email" in text
    assert "composio" not in text.lower()
    # a parked call's result is a model instruction — never quoted
    assert "held for approval (#" not in text
    # observations render like live events, not the raw log line
    assert "Also seen: Checked your email" in text
    assert "Because: Checked your email" in text
    # a line the loop repeated across steps renders once
    assert text.count("Sam asked for a confirmation") == 1
    # model-authored text: raw tool tokens transcribe, pasted JSON collapses
    assert "I will run sending email once approved" in text
    assert "msg-9" not in text
    # decided_by "user" reads as "you"
    assert "decided by you" in text

    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector('[data-testid="event-row"]')
    page.wait_for_timeout(700)
    text = page.locator("body").inner_text()
    # the chat handle rides under _sender — and resolves to a name
    assert "Chat with Sam on Telegram" in text
    # an agent-outcome event transcribes the composio tool and unwraps the
    # envelope — the raw JSON (log_id, successful) never reaches the screen
    assert "Aether · Checking the connected Email account" in text
    assert "sam@example.com" in text
    assert "log_id" not in text
    assert "successful" not in text
    # a legacy agent event without `plain` is backfilled by the real route
    assert "Aether · Sent the message (to @sam)" in text
    # shared media reads plainly and renders by its sniffed type — an
    # image, a video player, and a gone blob as the plain note
    assert "Sam shared an image — the-plan.png" in text
    assert "Sam shared a video — site-tour.mp4" in text
    assert "Sam shared a voice note — reminder.oga" in text
    assert "call the plumber before friday" in text
    assert page.locator('[data-testid="media-image"]').count() == 1
    assert page.locator('[data-testid="media-video"]').count() == 1
    assert page.locator('[data-testid="media-gone"]').count() == 1
    assert (
        page.locator('[data-testid="media-gone"]').inner_text().strip()
        == "(this file is no longer stored)"
    )

    page.goto(f"{live_server}/#/attention")
    page.wait_for_selector('[data-testid="approval-card"]')
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    assert "Completed after your approval" in text
    assert "Approved by you" in text
    assert "on You" not in text

    page.goto(f"{live_server}/#/memory")
    page.wait_for_timeout(800)
    assert "Relationship — coworker on the infra team" in page.locator("body").inner_text()

    page.goto(f"{live_server}/#/audit")
    page.wait_for_selector("table")
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    assert "Read-only; nothing was changed" in text
    # the background worker's lifecycle row reads plainly — actor, action
    # and outcome all through the vocabulary, never the raw tokens
    assert "Noted your verdict on background work" in text
    actors = page.locator("tbody tr td:nth-child(3)").all_inner_texts()
    assert all(
        a in ("Aether", "You", "Scheduled check", "Routine", "Background worker") for a in actors
    )

    page.goto(f"{live_server}/#/tasks")
    page.wait_for_selector('[data-testid="proactive-row"]')
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    # skill names read through the vocabulary — never the raw tokens
    assert "Following up on something you said" in text
    assert "Background research" in text
    assert "Notes on a file you shared" in text
    assert "followups" not in text.lower()
    assert "media" not in text.lower()
    # a settled row shows its verdict in plain words, and no buttons
    assert "Discarded" in text
    assert "too detailed" in text
    # the staged-approval row offers all three verdicts; the plain row two
    assert page.locator('[data-testid="proactive-approve-2"]').count() == 1
    assert page.locator('[data-testid="proactive-revise-2"]').count() == 1
    assert page.locator('[data-testid="proactive-discard-3"]').count() == 1
    assert page.locator('[data-testid="proactive-revise-3"]').count() == 0
    # the one-tap round trip: the note lands, the row settles, buttons go
    page.click('[data-testid="proactive-approve-2"]')
    page.wait_for_timeout(500)
    settled = page.locator("body").inner_text()
    assert "noted — I'll keep doing things like that." in settled
    assert "Approved" in settled
    assert page.locator('[data-testid="proactive-approve-2"]').count() == 0

    page.goto(f"{live_server}/#/settings")
    page.wait_for_timeout(800)
    text = page.locator("body").inner_text()
    assert '"warm and a little informal"' not in text
    assert "warm and a little informal" in text

    # the editable surface renders from the backend's feed: the model
    # group says it restarts, and every control carries its effective value;
    # the provider field itself is gone — every model name now carries its
    # own service in front
    assert "Saving one of these restarts Aether itself" in text
    assert page.locator('[data-testid="settings-field-llm.provider"]').count() == 0
    assert page.locator('[data-testid="settings-field-agent.tick_seconds"]').input_value() == "30"

    # a number edit round-trips through the config manager and earns the
    # changed marker and its way back
    page.fill('[data-testid="settings-field-agent.tick_seconds"]', "15")
    page.click('[data-testid="settings-save-agent.tick_seconds"]')
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="settings-field-agent.tick_seconds"]').input_value() == "15"
    assert page.locator('[data-testid="settings-reset-agent.tick_seconds"]').count() == 1
    page.click('[data-testid="settings-reset-agent.tick_seconds"]')
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="settings-field-agent.tick_seconds"]').input_value() == "30"
    assert page.locator('[data-testid="settings-reset-agent.tick_seconds"]').count() == 0

    # a bad quiet window refuses in the backend's own plain words, inline
    page.fill('[data-testid="settings-field-agent.quiet_hours"]', "banana")
    page.click('[data-testid="settings-save-agent.quiet_hours"]')
    page.wait_for_selector('[data-testid="settings-error-agent.quiet_hours"]')
    error_text = page.locator('[data-testid="settings-error-agent.quiet_hours"]').inner_text()
    assert "the quiet window reads like 23:00-08:00" in error_text

    # a pasted credential flips its badge; the value never reaches the page
    assert (
        page.locator('[data-testid="settings-cred-set-DISCORD_BOT_TOKEN"]').inner_text()
        == "not stored"
    )
    page.fill('[data-testid="settings-cred-DISCORD_BOT_TOKEN"]', "discord-token-9")
    page.click('[data-testid="settings-cred-save-DISCORD_BOT_TOKEN"]')
    page.wait_for_timeout(500)
    assert (
        page.locator('[data-testid="settings-cred-set-DISCORD_BOT_TOKEN"]').inner_text() == "stored"
    )
    # write-only: cleared from the field, absent from body text
    assert page.locator('[data-testid="settings-cred-DISCORD_BOT_TOKEN"]').input_value() == ""
    assert "discord-token-9" not in page.locator("body").inner_text()

    # the profile card: confirmed entries attributed plainly, pending ones
    # waiting for one yes — the same flip the chat confirmation makes
    page.wait_for_selector('[data-testid="profile-card"]')
    profile_text = page.locator("body").inner_text()
    assert "What Aether knows about you" in profile_text
    assert "Aether noticed" in profile_text
    assert "you are building a personal assistant you call Aether" in profile_text
    assert page.locator('[data-testid="profile-pending-p-paper"]').count() == 1
    page.click('[data-testid="profile-keep-p-paper"]')
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="profile-pending-p-paper"]').count() == 0
    assert page.locator('[data-testid="profile-confirmed-p-paper"]').count() == 1
    assert "Noted." in page.locator("body").inner_text()

    page.goto(f"{live_server}/#/contacts")
    page.wait_for_selector('[data-testid="contact-row"]')
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    # the allowlist renders people plainly — a name over the platform and
    # handle, and the "*" platform reads as "Any platform", never a wildcard
    assert "Sam" in text
    assert "Any platform" in text
    # each connected app has its own card with its own people
    assert page.locator('[data-testid="app-card-gmail"]').count() == 1
    assert page.locator('[data-testid="app-card-telegram"]').count() == 1
    assert "abc123@xyz.com" in page.locator('[data-testid="app-card-gmail"]').inner_text()
    assert "@sam" in page.locator('[data-testid="app-card-telegram"]').inner_text()
    # a disconnected app with no list of its own never shows a card
    assert page.locator('[data-testid="app-card-discord"]').count() == 0

    # one app's own switch round-trips: only its list, then back to default
    page.select_option('[data-testid="app-mode-telegram"]', "enforce")
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="app-mode-telegram"]').input_value() == "enforce"
    page.select_option('[data-testid="app-mode-telegram"]', "default")
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="app-mode-telegram"]').input_value() == "default"

    # the panel's one write path round-trips inside one app's card: add then
    # remove, both through the config manager, both landing on the audit trail
    page.fill('[data-testid="contact-handle-telegram"]', "@priya")
    page.click('[data-testid="contact-add-telegram"]')
    page.wait_for_timeout(500)
    card_rows = page.locator('[data-testid="contact-row-telegram"]')
    assert card_rows.count() == 2
    card_rows.nth(1).locator('[data-testid="contact-remove"]').click()
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="contact-row-telegram"]').count() == 1

    # and the same round-trip on the list that counts everywhere
    page.fill('[data-testid="contact-handle"]', "@priya")
    page.click('[data-testid="contact-add"]')
    page.wait_for_timeout(500)
    rows = page.locator('[data-testid="contact-row"]')
    assert rows.count() == 2
    rows.nth(1).locator('[data-testid="contact-remove"]').click()
    page.wait_for_timeout(500)
    assert page.locator('[data-testid="contact-row"]').count() == 1


def test_approval_cards_show_the_full_ask(page, live_server, token) -> None:
    # The pending card renders everything the request carries, from its
    # own shape: labels come from the request's own keys, the format from
    # the content itself, long text scrolls and expands instead of
    # cutting, and a decision applies to exactly what is on screen.
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")
    page.goto(f"{live_server}/#/attention")
    page.wait_for_selector('[data-testid="approval-card"]')
    page.wait_for_timeout(500)
    cards = page.locator('[data-testid="approval-card"]')
    assert cards.count() == 6

    # each card names the action in plain words, then shows the request's
    # own fields in its own order — no separate summary line repeats them
    mail = cards.nth(0)
    mail_text = mail.inner_text()
    assert "Sending messages on Email" in mail_text
    assert "To" in mail_text and "sam@example.com" in mail_text

    issue = cards.nth(2)
    issue_text = issue.inner_text()
    assert "Opening the GitHub issue" in issue_text
    assert "Title" in issue_text and "Deploy script fails on the nightly build" in issue_text

    event = cards.nth(3)
    event_text = event.inner_text()
    assert "Creating the event" in event_text
    assert "Title" in event_text and "Dentist" in event_text

    patch = cards.nth(4)
    patch_text = patch.inner_text()
    assert "Apply patch in Patch" in patch_text
    assert "Path" in patch_text and "scripts/deploy.sh" in patch_text

    # a long issue body previews its opening; its end is one tap away,
    # the box says its size out loud, and the whole read stays in place
    assert "no space left on device" not in issue_text
    assert "whether it happened again" not in issue_text
    expand = issue.locator('[data-testid="approval-expand"]')
    assert expand.inner_text().strip() == "Read full text"
    assert (
        issue.locator('[data-testid="approval-text-size"]')
        .inner_text()
        .strip()
        .endswith("characters")
    )
    expand.click()
    page.wait_for_timeout(100)
    assert issue.locator('[data-testid="approval-expand"]').inner_text().strip() == "Show less"
    issue_text = issue.inner_text()
    assert "no space left on device" in issue_text
    assert "whether it happened again" in issue_text

    # fenced code renders as a code block with the fence markers gone, a
    # list of items numbers itself, and an empty field says so
    assert "```" not in issue_text
    assert issue.locator('[data-testid="approval-code"]').count() == 1
    assert issue.locator('[data-testid="approval-items"]').count() == 1
    assert "(empty)" in issue_text
    expand.click()
    page.wait_for_timeout(100)
    assert issue.locator('[data-testid="approval-expand"]').inner_text().strip() == "Read full text"

    # the calendar row: a timestamp reads as a date, attendees number
    # their own labelled fields, an empty string says so
    assert "Oct 28, 2026" in event_text
    assert "2026-10-28T09:00:00" not in event_text
    assert "Sam Rivera" in event_text and "priya@example.com" in event_text
    assert "(empty)" in event_text

    # the unrecognized tool: a path reads in monospace, a link stays a
    # link, and a diff keeps its shape with both sides readable
    assert patch.locator('[data-testid="approval-path"]').count() == 1
    assert patch.locator("a").count() == 1
    assert patch.locator("a").get_attribute("href") == "https://example.com/deploys/rollback-plan"
    assert patch.locator('[data-testid="approval-diff"]').count() == 1
    assert "deploying the nightly release" in patch_text
    assert "deploying the tagged release" in patch_text

    # the gmail reply: the card names the action and its own fields; the
    # thread it answers is an internal reference pointing at the decision
    # record's exact copy, and the raw token never reaches the screen
    reply = cards.nth(5)
    reply_text = reply.inner_text()
    assert "Replying to email" in reply_text
    assert "Message" in reply_text and "Sounds good — Thursday works." in reply_text
    assert "Thread id" in reply_text
    assert "(kept in the decision record)" in reply_text
    assert reply.locator('[data-testid="approval-reference"]').count() == 1
    assert "19c2e6fe0b2f1a34" not in page.locator("body").inner_text()

    # the decision buttons ride below the capped content, always in view
    assert patch.locator('[data-testid="approve"]').count() == 1
    assert patch.locator('[data-testid="deny"]').count() == 1

    # and the whole page still speaks plainly
    _scan(page.locator("body").inner_text(), "attention (full asks)")


def test_model_fields_are_pickers_over_the_live_catalog(page, live_server, token) -> None:
    # the six model knobs are dropdowns over the fetched catalog: no
    # free-text entry remains, both configured providers list in one
    # dropdown in the provider-prefixed form, and the sentinel words read
    # as what they mean — never as raw knob values on the screen
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")
    page.goto(f"{live_server}/#/settings")
    page.wait_for_selector('[data-testid="settings-field-llm.model"]')

    # every model field is the picker's trigger button — the old free-text
    # inputs are gone, and so is the provider field: every model name now
    # carries its own service in front
    for path in (
        "llm.model",
        "llm.image_model",
        "llm.audio_model",
        "llm.video_model",
        "llm.salience_model",
        "llm.embedding_model",
    ):
        trigger = page.locator(f'[data-testid="settings-field-{path}"]')
        assert trigger.evaluate("el => el.tagName") == "BUTTON"
    assert page.locator('[data-testid="settings-field-llm.provider"]').count() == 0

    # unset fields show their meaning, not a blank: a medium with nothing
    # picked is "None (off)", an unset first-pass skim is "Use default"
    assert (
        page.locator('[data-testid="settings-field-llm.image_model"]').inner_text().strip()
        == "None (off)"
    )
    assert (
        page.locator('[data-testid="settings-field-llm.salience_model"]').inner_text().strip()
        == "Use default"
    )

    # the fixture's main model was typed before the dropdown existed — it
    # is kept as saved and said so plainly, never reset to a listed one;
    # the embedding default, a legacy bare id the catalog still serves,
    # reads as itself with no such note
    page.wait_for_selector('[data-testid="settings-model-unknown-llm.model"]')
    assert (
        page.locator('[data-testid="settings-field-llm.model"]').inner_text().strip()
        == "claude-sonnet-5"
    )
    assert (
        "not in the available models — kept as saved"
        in page.locator('[data-testid="settings-model-unknown-llm.model"]').inner_text()
    )
    assert (
        page.locator('[data-testid="settings-field-llm.embedding_model"]').inner_text().strip()
        == "gemini-embedding-001"
    )
    assert page.locator('[data-testid="settings-model-unknown-llm.embedding_model"]').count() == 0

    # opening the main model's picker: both providers' models in one
    # dropdown, grouped under their provider, in the stored display form
    page.click('[data-testid="settings-field-llm.model"]')
    page.wait_for_selector('[data-testid="settings-model-list-llm.model"]')
    model_list = page.locator('[data-testid="settings-model-list-llm.model"]')
    assert "ANTHROPIC" in model_list.inner_text()
    assert "GEMINI" in model_list.inner_text()
    assert "NVIDIA" in model_list.inner_text()
    assert (
        model_list.locator('[data-testid="settings-model-option-anthropic/claude-opus-5"]')
        .inner_text()
        .strip()
        == "anthropic/claude-opus-5"
    )
    assert (
        model_list.locator('[data-testid="settings-model-option-gemini/gemini-2.5-flash"]').count()
        == 1
    )
    # NVIDIA's vendor ids pool into the same dropdown, in the prefixed form
    # the registry routes
    assert (
        model_list.locator(
            '[data-testid="settings-model-option-nvidia/meta/llama-3.2-90b-vision-instruct"]'
        ).count()
        == 1
    )
    # the typed value rides in the list too — kept selectable, never dropped
    assert (
        model_list.locator('[data-testid="settings-model-option-unknown-llm.model"]').count() == 1
    )

    # the filter narrows to what matches
    page.fill('[data-testid="settings-model-search-llm.model"]', "gemini-2.5")
    page.wait_for_timeout(200)
    assert (
        model_list.locator('[data-testid="settings-model-option-anthropic/claude-opus-5"]').count()
        == 0
    )
    assert (
        model_list.locator('[data-testid="settings-model-option-gemini/gemini-2.5-flash"]').count()
        == 1
    )
    # and it forgives the way people say a name: spaced and dotted finds
    # the dashed id — no exact typing anywhere
    page.fill('[data-testid="settings-model-search-llm.model"]', "gemini 3.1 flash")
    page.wait_for_timeout(200)
    assert (
        model_list.locator(
            '[data-testid="settings-model-option-gemini/gemini-3.1-flash-lite"]'
        ).count()
        == 1
    )
    assert (
        model_list.locator('[data-testid="settings-model-option-anthropic/claude-opus-5"]').count()
        == 0
    )
    page.fill('[data-testid="settings-model-search-llm.model"]', "")

    # the refresh button refetches the catalog — the fake's held-back model
    # appears, proving the round trip rides all the way to the provider
    page.click('[data-testid="settings-models-refresh-llm.model"]')
    page.wait_for_selector(
        '[data-testid="settings-model-list-llm.model"] '
        '[data-testid="settings-model-option-anthropic/claude-opus-4-1"]'
    )

    # picking a model stores the full prefixed form and saves it — the
    # trigger keeps reading it, the change is marked, the note is gone
    page.click('[data-testid="settings-model-option-gemini/gemini-2.5-flash"]')
    assert (
        page.locator('[data-testid="settings-field-llm.model"]').inner_text().strip()
        == "gemini/gemini-2.5-flash"
    )
    page.click('[data-testid="settings-save-llm.model"]')
    page.wait_for_timeout(500)
    assert (
        page.locator('[data-testid="settings-field-llm.model"]').inner_text().strip()
        == "gemini/gemini-2.5-flash"
    )
    assert page.locator('[data-testid="settings-reset-llm.model"]').count() == 1
    assert page.locator('[data-testid="settings-model-unknown-llm.model"]').count() == 0

    # a perception knob offers both its meanings, and picking one stores
    # the word behind it — "Use default" for the main model's own eyes
    page.click('[data-testid="settings-field-llm.image_model"]')
    page.wait_for_selector('[data-testid="settings-model-list-llm.image_model"]')
    image_list = page.locator('[data-testid="settings-model-list-llm.image_model"]')
    # the same pooled catalog feeds a capability field — NVIDIA's vision
    # model is offered here without a provider-specific picker
    assert (
        image_list.locator(
            '[data-testid="settings-model-option-nvidia/meta/llama-3.2-90b-vision-instruct"]'
        ).count()
        == 1
    )
    assert (
        image_list.locator('[data-testid="settings-model-option-off"]').inner_text().strip()
        == "None (off)"
    )
    assert (
        image_list.locator('[data-testid="settings-model-option-default"]').inner_text().strip()
        == "Use default"
    )
    page.click('[data-testid="settings-model-option-default"]')
    page.click('[data-testid="settings-save-llm.image_model"]')
    page.wait_for_timeout(500)
    assert (
        page.locator('[data-testid="settings-field-llm.image_model"]').inner_text().strip()
        == "Use default"
    )

    # "None (off)" is pickable the same way and disables the medium
    page.click('[data-testid="settings-field-llm.audio_model"]')
    page.wait_for_selector('[data-testid="settings-model-list-llm.audio_model"]')
    page.click(
        '[data-testid="settings-model-list-llm.audio_model"] [data-testid="settings-model-option-off"]'
    )
    page.click('[data-testid="settings-save-llm.audio_model"]')
    page.wait_for_timeout(500)
    assert (
        page.locator('[data-testid="settings-field-llm.audio_model"]').inner_text().strip()
        == "None (off)"
    )

    # each field offers only the meanings it can really take: the
    # first-pass skim has both its meanings, memory search is a forced
    # choice — no off, no default, only real models
    page.click('[data-testid="settings-field-llm.salience_model"]')
    page.wait_for_selector('[data-testid="settings-model-list-llm.salience_model"]')
    salience_list = page.locator('[data-testid="settings-model-list-llm.salience_model"]')
    assert salience_list.locator('[data-testid="settings-model-option-default"]').count() == 1
    assert salience_list.locator('[data-testid="settings-model-option-off"]').count() == 1
    # close the open dropdown (its trigger toggles) and clear the sticky
    # header before the next one — the open list overlays the row below
    page.click('[data-testid="settings-field-llm.salience_model"]')
    page.wait_for_selector(
        '[data-testid="settings-model-list-llm.salience_model"]', state="detached"
    )
    page.mouse.wheel(0, 120)
    page.click('[data-testid="settings-field-llm.embedding_model"]')
    page.wait_for_selector('[data-testid="settings-model-list-llm.embedding_model"]')
    embedding_list = page.locator('[data-testid="settings-model-list-llm.embedding_model"]')
    assert embedding_list.locator('[data-testid="settings-model-option-off"]').count() == 0
    assert embedding_list.locator('[data-testid="settings-model-option-default"]').count() == 0
    assert (
        embedding_list.locator(
            '[data-testid="settings-model-option-gemini/gemini-embedding-001"]'
        ).count()
        == 1
    )

    # and the open dropdown speaks the same plain language as the rest
    _scan(page.locator("body").inner_text(), "settings (model picker open)")
