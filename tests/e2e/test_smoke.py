"""Browser E2E smoke tests against the real frontend + real FastAPI routes,
backed by the in-memory dev fixture (tests/e2e/dev_server.py) instead of
Postgres. Opt-in: set AETHER_TEST_E2E=1 and run `playwright install
chromium` once first — see CONTRIBUTING.md.

These replace manual screenshot-driven verification with something that
fails loudly and repeatably: real navigation, a real approve round-trip
against the fake backend, real filtering of real DOM rows, and a real
reload to prove persistence.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _set_token(page, token: str) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")


def test_every_nav_route_loads(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/control")
    page.wait_for_selector("#chat-log")

    routes = {
        "activity": "Live Activity",
        "attention": "Attention Required",
        "tasks": "Tasks",
        "memory": "Memory & Context",
        "audit": "Audit Log",
        "policy": "Authorization & Policies",
        "apps": "Apps & Permissions",
        "settings": "Settings",
    }
    for path, title in routes.items():
        page.goto(f"{live_server}/#/{path}")
        page.wait_for_selector(f"h1#page-title:has-text('{title}')")
    assert page.locator(".toast").count() == 0  # no error toasts along the way


def test_approve_reaches_the_fake_backend_and_the_card_disappears(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/attention")
    page.wait_for_selector(".approval-card")
    assert page.locator(".approval-card").count() == 2

    first_tool = page.locator(".approval-card .tool").first.inner_text()
    page.locator(".approval-card").first.get_by_text("Approve").click()
    page.wait_for_selector(".approval-card .result.ok")
    page.wait_for_timeout(
        1500
    )  # the deliberately-delayed post-decide refetch (see app.js decide())

    remaining = page.locator(".approval-card .tool").all_inner_texts()
    assert first_tool not in remaining
    assert len(remaining) == 1


def test_activity_source_filter_actually_filters(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector(".list-item")
    assert page.locator(".list-item").count() == 2

    page.select_option("#f-source", "mail")
    rows = page.locator(".list-item")
    expect_count = rows.count()
    assert expect_count == 1
    assert "MAIL" in rows.first.inner_text().upper()

    page.select_option("#f-source", "")
    assert page.locator(".list-item").count() == 2


def test_personality_text_survives_a_full_reload(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/settings")
    page.wait_for_selector("#s-personality")

    text = "Be extremely terse. Never use emoji."
    page.fill("#s-personality", text)
    page.click("#s-personality-save")
    page.wait_for_selector("#s-personality-status:has-text('saved')")

    page.reload()
    page.wait_for_selector("#s-personality")
    assert page.input_value("#s-personality") == text


def test_no_token_shows_the_honest_empty_state_not_a_crash(page, live_server) -> None:
    page.goto(f"{live_server}/#/attention")
    page.wait_for_selector("text=No API token set")
    assert page.locator(".approval-card").count() == 0
