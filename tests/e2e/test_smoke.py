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
    # boot the SPA on a real page first, then hop routes
    page.goto(f"{live_server}/#/activity")
    page.wait_for_selector("h1#page-title")

    routes = {
        "activity": "Live Activity",
        "attention": "Attention Required",
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


def test_trace_row_expands_to_show_its_detail(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/traces")
    page.wait_for_selector(".trace-row")
    assert page.locator(".trace-row").count() == 4  # one per kind in the dev fixture

    # expand the turn trace — the detail lazy-fetches /api/traces/1
    page.locator(".trace-row .trace-head", has_text="telegram · @sam").click()
    # wait for real detail rows — the skeleton matches .trace-detail while the fetch is in flight
    page.wait_for_selector(".trace-row.open .trace-detail .trace-line")
    detail = page.locator(".trace-row.open .trace-detail")
    assert "can you confirm thursday at 4?" in detail.inner_text()
    assert "approval #1" in detail.inner_text()
    assert page.locator(".trace-row.open .trace-call").count() == 2

    # clicking again collapses it
    page.locator(".trace-row.open .trace-head").click()
    assert page.locator(".trace-row.open").count() == 0


def test_the_hub_grid_searches_connects_and_disconnects(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector("#hub-grid .available-card")

    # one grid, every app — the connected one carries its identity and sorts first
    cards = page.locator("#hub-grid .available-card")
    assert cards.count() == 3
    assert "Gmail" in cards.first.inner_text()
    assert "me@example.com" in cards.first.inner_text()
    assert cards.first.locator("button", has_text="Disconnect").is_visible()
    assert page.locator("#hub-grid .available-card", has_text="GitHub").locator(
        "button", has_text="Connect"
    ).is_visible()
    assert page.locator("#hub-grid .letter-tile").first.is_visible()  # no logo → letter

    # the search filters the grid client-side
    page.fill(".hub-search", "not")
    assert page.locator("#hub-grid .available-card").count() == 1
    assert page.locator("#hub-grid .available-card", has_text="Notion").is_visible()
    page.fill(".hub-search", "zzz")
    assert page.locator("#hub-grid .empty-state").is_visible()
    page.fill(".hub-search", "")

    # connect POSTs and opens the hub's link in a new tab (stubbed)
    # function form, not a bare assignment — an evaluate string whose completion
    # value is the arrow gets *invoked* by playwright, pushing a phantom undefined
    page.evaluate("() => { window.__opened = []; window.open = (u) => window.__opened.push(u); }")
    page.locator("#hub-grid .available-card", has_text="GitHub").locator("button").click()
    page.wait_for_function("window.__opened.length === 1")
    assert page.evaluate("window.__opened[0]") == "https://hub.example.test/connect/github"

    # disconnect flips the card in place — the app stays in the grid, now connectable
    page.locator("#hub-grid .available-card", has_text="Gmail").locator("button").click()
    page.wait_for_function(
        """() => {
          const card = [...document.querySelectorAll('#hub-grid .available-card')]
            .find((c) => c.textContent.includes('Gmail'));
          return card && card.querySelector('button')
            && card.querySelector('button').textContent === 'Connect';
        }"""
    )
    assert page.locator("#hub-grid .available-card").count() == 3


def test_no_token_shows_the_honest_empty_state_not_a_crash(page, live_server) -> None:
    page.goto(f"{live_server}/#/attention")
    page.wait_for_selector("text=No API token set")
    assert page.locator(".approval-card").count() == 0
