"""The direct-provider panel walk: Gmail setup, the callback, Drive's file
picker setup, LinkedIn setup, and disconnect.

The dev fixture's provider runtime is the real route surface with in-memory
Gmail, Calendar, Drive, and Docs cards on one Google connection, plus GitHub
and LinkedIn cards. The OAuth browser tab is skipped by landing the callback
directly, which is the same state-only route the provider calls.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _set_token(page, token: str) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")


def test_github_token_card_connects_and_disconnects(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-github"]')

    card = page.locator('[data-testid="provider-github"]')
    assert "Not connected" in card.inner_text()
    assert "Use browser sign-in" in card.inner_text()

    card.get_by_role("button", name="Connect").click()
    card.get_by_placeholder("GitHub fine-grained personal access token").wait_for()
    assert "Fine-grained tokens" in card.inner_text()
    assert "Open the token page" in card.inner_text()
    card.get_by_placeholder("GitHub fine-grained personal access token").fill("github_pat_e2e")
    card.locator('input[type="date"]').fill("2026-12-31")
    card.get_by_role("button", name="Save token").click()

    page.wait_for_selector('[data-testid="provider-github"]:has-text("octocat")')
    card = page.locator('[data-testid="provider-github"]')
    assert "octocat" in card.inner_text()
    assert "Token connection" in card.inner_text()
    assert "Use browser sign-in" in card.inner_text()

    card.get_by_role("button", name="Disconnect").click()
    page.wait_for_selector('[data-testid="provider-github"]:has-text("Not connected")')


def test_drive_picker_setup_is_saved_from_the_panel(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-gmail"]')

    card = page.locator('[data-testid="provider-gmail"]')
    form = card.locator("form")
    form.get_by_placeholder("Client ID").fill("client-id.apps.googleusercontent.com")
    form.get_by_placeholder("Client secret").fill("client-secret")
    form.get_by_role("button", name="Save client").click()
    page.wait_for_selector('[data-testid="provider-gmail"]:has-text("Not connected")')

    page.goto(f"{live_server}/api/oauth/google/callback?code=e2e&state=e2e")
    page.wait_for_selector('[data-testid="provider-drive"]:has-text("Connected")')

    picker = page.locator('[data-testid="drive-picker"]')
    picker.get_by_placeholder("Google API key").fill("picker-api-key")
    picker.get_by_placeholder("Google Cloud project number").fill("123456")
    picker.get_by_role("button", name="Save picker setup").click()
    page.wait_for_selector('[data-testid="drive-picker"]:has-text("Choose files")')
    assert "Choose the Google Docs" in picker.inner_text()


def test_linkedin_setup_callback_note_and_disconnect(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-linkedin"]')

    card = page.locator('[data-testid="provider-linkedin"]')
    assert "Setup needed" in card.inner_text()
    assert "60 days" in card.inner_text()

    form = card.locator("form")
    form.get_by_placeholder("Client ID").fill("linkedin-client-id")
    form.get_by_placeholder("Client secret").fill("linkedin-client-secret")
    form.get_by_role("button", name="Save client").click()
    page.wait_for_selector('[data-testid="provider-linkedin"]:has-text("Not connected")')

    page.goto(f"{live_server}/api/oauth/linkedin/callback?code=e2e&state=e2e")
    page.wait_for_selector('[data-testid="provider-linkedin"]:has-text("Connected")')
    card = page.locator('[data-testid="provider-linkedin"]')
    assert "ada@example.com" in card.inner_text()
    assert "60 days" in card.inner_text()

    card.get_by_role("button", name="Disconnect").click()
    page.wait_for_selector('[data-testid="provider-linkedin"]:has-text("Not connected")')


def test_direct_gmail_setup_callback_and_disconnect(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-gmail"]')

    card = page.locator('[data-testid="provider-gmail"]')
    assert "Setup needed" in card.inner_text()
    assert "Add this redirect URI exactly" in card.inner_text()
    assert "GOOGLE_CLIENT_ID" not in card.inner_text()  # setup steps are plain
    calendar = page.locator('[data-testid="provider-calendar"]')
    assert "Google Calendar" in calendar.inner_text()
    assert "Setup needed" in calendar.inner_text()

    form = card.locator("form")
    form.get_by_placeholder("Client ID").fill("client-id.apps.googleusercontent.com")
    form.get_by_placeholder("Client secret").fill("client-secret")
    form.get_by_role("button", name="Save client").click()

    page.wait_for_selector('[data-testid="provider-gmail"]:has-text("Not connected")')

    # the browser would land here after consent; the fake runtime stores
    # the connection and the real route redirects back to the panel
    page.goto(f"{live_server}/api/oauth/google/callback?code=e2e&state=e2e")
    page.wait_for_selector('[data-testid="provider-gmail"]:has-text("Connected")')
    card = page.locator('[data-testid="provider-gmail"]')
    assert "me@example.com" in card.inner_text()
    assert "Connected" in page.locator('[data-testid="provider-calendar"]').inner_text()

    card.get_by_role("button", name="Disconnect").click()
    page.wait_for_selector('[data-testid="provider-gmail"]:has-text("Not connected")')
    assert "Not connected" in page.locator('[data-testid="provider-calendar"]').inner_text()
