"""Open the page that issues a key while the wizard asks for it.

A key prompt carries the exact console or dashboard URL in its hint. Opening
that URL saves the user from copying it out of the terminal; the hint still
prints, so a machine with no browser (SSH, a server, a locked-down desktop)
shows the same link. Failures never interrupt setup.

``AETHER_SETUP_NO_BROWSER`` turns the automatic open off for a user who
prefers it. The unit suite turns it off through ``use_browser(False)``.
"""

from __future__ import annotations

import os
import re
import webbrowser

_OPEN = True
_URL = re.compile(r"https?://[^\s]+")


def use_browser(enabled: bool) -> None:
    """Turn the automatic open on or off. Off is the hermetic test mode."""
    global _OPEN
    _OPEN = enabled


def open_link(url: str) -> bool:
    """Open a page in the default browser. Returns whether it was launched."""
    if not _OPEN or not url or os.environ.get("AETHER_SETUP_NO_BROWSER"):
        return False
    try:
        return webbrowser.open(url, new=2)
    except Exception:
        return False


def open_hint(hint: str) -> bool:
    """Open the first URL a hint carries, if it has one."""
    match = _URL.search(hint or "")
    return open_link(match.group(0)) if match else False
