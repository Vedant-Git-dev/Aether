"""Small local-clock helpers shared by the loop and the proactive path.

Quiet hours are the user's own wall clock, not UTC, so the parse and the
window test live here once. A malformed value is logged and ignored: a
budget the config cannot explain is worse than no budget.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("aether.memory.timewindow")


def parse_quiet_hours(raw: str | None) -> tuple[int, int] | None:
    """Parse "HH:MM-HH:MM" into minutes since midnight.

    The window may wrap midnight ("23:00-08:00"); a malformed value is
    logged and ignored."""
    if not raw:
        return None
    start_text, sep, end_text = raw.partition("-")
    try:
        if not sep:
            raise ValueError(f"quiet_hours {raw!r} must look like 'HH:MM-HH:MM'")
        start_h, start_m = (int(part) for part in start_text.strip().split(":"))
        end_h, end_m = (int(part) for part in end_text.strip().split(":"))
        window = (start_h * 60 + start_m, end_h * 60 + end_m)
        if not all(0 <= minute < 24 * 60 for minute in window):
            raise ValueError(f"quiet_hours {raw!r} is outside a day")
        return window
    except ValueError as exc:
        log.warning("%s — quiet hours disabled", exc)
        return None


def in_quiet_window(window: tuple[int, int] | None, now: datetime) -> bool:
    """True when the naive local time falls inside the window."""
    if window is None:
        return False
    minutes = now.hour * 60 + now.minute
    start, end = window
    if start == end:
        return True  # a degenerate window is a 24h hold — config's choice
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # wraps midnight


def quiet_hours_match(raw: str | None, now: datetime) -> bool:
    """One raw window string against a naive local time."""
    return in_quiet_window(parse_quiet_hours(raw), now)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def local_clock(now: datetime, timezone: str) -> str:
    """HH:MM in the user's timezone, for a live result's "as of"."""
    try:
        return now.astimezone(zone(timezone)).strftime("%H:%M")
    except (KeyError, ValueError):
        return now.strftime("%H:%M UTC")


def local_stamp(now: datetime, timezone: str) -> str:
    """A date and time the user can read, with the zone's short name."""
    in_zone = now.astimezone(zone(timezone))
    label = in_zone.tzname() or ""
    return f"{in_zone:%Y-%m-%d %H:%M}" + (f" {label}" if label else "")
