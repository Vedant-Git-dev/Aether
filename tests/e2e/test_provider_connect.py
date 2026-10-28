"""The direct-provider panel walk: Gmail setup, the callback, and disconnect.

The dev fixture's provider runtime is the real route surface with an
in-memory Gmail card; the OAuth browser tab is skipped by landing the
callback directly, which is the same state-only route Google calls.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _set_token(page, token: str) -> None:
    page.add_init_script(f"localStorage.setItem('aether_token', '{token}')")


def test_direct_gmail_setup_callback_and_disconnect(page, live_server, token) -> None:
    _set_token(page, token)
    page.goto(f"{live_server}/#/apps")
    page.wait_for_selector('[data-testid="provider-gmail"]')

    card = page.locator('[data-testid="provider-gmail"]')
    assert "Setup needed" in card.inner_text()
    assert "Add this redirect URI exactly" in card.inner_text()
    assert "GOOGLE_CLIENT_ID" not in card.inner_text()  # setup steps are plain

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

    card.get_by_role("button", name="Disconnect").click()
    page.wait_for_selector('[data-testid="provider-gmail"]:has-text("Not connected")')
