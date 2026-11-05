"""Browser E2E smoke tests against the real frontend + real FastAPI routes,
backed by the in-memory dev fixture (tests/e2e/dev_server.py) instead of
Postgres. Opt-in: set AETHER_TEST_E2E=1 and run `playwright install
chromium` once first — see CONTRIBUTING.md.

These replace manual screenshot-driven verification with something that
fails loudly and repeatably: real navigation, a real approve round-trip
against the fake backend, real filtering of real DOM rows, and a real
reload to prove persistence. Selectors are data-testid based — the panel
is a React app now (source in webapp/), and every interactive element it
exposes for these tests carries a stable testid.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _set_token(page, token: str) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")


def test_every_nav_route_loads(page, live_server, token) -> None:
    _set_token(page, token)
    # boot the SPA on a real page first, then hop routes
    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector('[data-testid="page-title"]')

    routes = {
        "activity": "Live Activity",
        "tasks": "Tasks",
        "memory": "Memory & Context",
        "audit": "Audit Log",
        "traces": "Decision Traces",
        "policy": "Authorization & Policies",
        "apps": "Apps & Permissions",
        "settings": "Settings",
    }
    for path, title in routes.items():
        page.goto(f"{live_server}/#/{path}")
        page.wait_for_selector(f'[data-testid="page-title"]:has-text("{title}")')
    assert page.locator('[data-testid="toast"]').count() == 0  # no error toasts along the way


def test_apps_page_hides_settings_and_the_legacy_hub(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-grid"]')
    page.wait_for_selector('[data-testid="integration-s-mail"]')

    # screen vision and the contact allowlist are features, not apps —
    # they belong on their own settings surfaces, not this page
    assert page.locator('[data-testid="integration-screen-vision"]').count() == 0
    assert page.locator('[data-testid="integration-contacts"]').count() == 0
    # the old app hub is gone even if its config entry is still present
    assert page.locator('[data-testid="integration-s-composio"]').count() == 0

    # the connected chat surfaces box lists only what is on: Telegram once,
    # never the disabled Discord, which belongs to the available list
    assert page.locator('[data-testid="integration-c-telegram"]').count() == 1
    assert page.locator('[data-testid="integration-c-discord"]').count() == 0
    assert page.locator('[data-testid="available-discord"]').count() == 1
    text = page.locator("body").inner_text().upper()
    assert "CONNECTED CHAT SURFACES" in text
    assert "APPS AETHER ACTS THROUGH" in text
    assert "AVAILABLE CHAT SURFACES" in text


def test_proactive_task_decision_reaches_the_fake_backend(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/tasks")
    page.wait_for_selector('[data-testid="proactive-row"]')
    assert page.locator('[data-testid="proactive-approve-2"]').count() == 1

    page.locator('[data-testid="proactive-approve-2"]').click()
    page.wait_for_timeout(500)

    # the verdict button is gone once decided
    assert page.locator('[data-testid="proactive-approve-2"]').count() == 0


def test_activity_source_filter_actually_filters(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector('[data-testid="event-row"]')
    assert page.locator('[data-testid="event-row"]').count() == 7

    page.select_option('[data-testid="filter-source"]', "mail")
    rows = page.locator('[data-testid="event-row"]')
    assert rows.count() == 1
    assert "EMAIL" in rows.first.inner_text().upper()  # the mail source is labelled "Email"

    page.select_option('[data-testid="filter-source"]', "")
    assert page.locator('[data-testid="event-row"]').count() == 7


def test_personality_text_survives_a_full_reload(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/settings")
    page.wait_for_selector('[data-testid="personality-input"]')

    text = "Be extremely terse. Never use emoji."
    page.fill('[data-testid="personality-input"]', text)
    page.click('[data-testid="personality-save"]')
    page.wait_for_selector('[data-testid="personality-status"]:has-text("Saved")')

    page.reload()
    page.wait_for_selector('[data-testid="personality-input"]')
    assert page.input_value('[data-testid="personality-input"]') == text


def test_trace_row_expands_to_show_its_detail(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/traces")
    page.wait_for_selector('[data-testid="trace-row"]')
    assert page.locator('[data-testid="trace-row"]').count() == 4  # one per kind in the dev fixture

    # expand the turn trace — the detail lazy-fetches /api/traces/1
    page.locator('[data-testid="trace-head"]', has_text="can you confirm thursday at 4?").click()
    # wait for real detail rows — the Loading… text stands in while the fetch is in flight
    page.wait_for_selector('[data-testid="trace-row"][data-open="1"] [data-testid="trace-line"]')
    detail = page.locator('[data-testid="trace-row"][data-open="1"]')
    assert "can you confirm thursday at 4?" in detail.inner_text()
    assert "approval #1" in detail.inner_text()
    assert detail.locator('[data-testid="trace-call"]').count() == 2

    # clicking again collapses it
    page.locator('[data-testid="trace-row"][data-open="1"] [data-testid="trace-head"]').click()
    assert page.locator('[data-testid="trace-row"][data-open="1"]').count() == 0


def test_no_token_shows_the_honest_empty_state_not_a_crash(page, live_server) -> None:
    page.goto(f"{live_server}/#/tasks")
    page.wait_for_selector("text=No API token set")
    assert page.locator('[data-testid="proactive-row"]').count() == 0
