"""The Aether brand assets, in one place.

Change the wordmark, the tagline, the colors or the timing here and the intro
and every styled prompt follow. Nothing else hardcodes brand characters.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.text import Text

# The wordmark. The Unicode form is preferred; the ASCII form is the fallback
# for terminals whose font or encoding cannot show the block glyphs.
UNICODE_LOGO: tuple[str, ...] = (
    "  ▄▀█ █▀▀ ▀█▀ █ █ █▀▀ █▀█",
    "  █▀█ ██▄  █  █▀█ ██▄ █▀▄",
)
ASCII_LOGO: tuple[str, ...] = (
    "   /\\  | __|_   _| |_  _ __ ___ ",
    "  / _\\ | _| | | | ' \\| '_ \\/ -_)",
    " /_/ \\_\\___| |_| |_||_| .__/\\___|",
    "                      |_|        ",
)

TAGLINE = "your continuous, cross-platform personal action agent"

# The gradient, top to bottom. Kept short so the fade is subtle. Change these
# two to re-skin the whole intro.
GRADIENT_TOP = "#8b5cf6"  # violet
GRADIENT_BOTTOM = "#ec4899"  # pink
TAGLINE_STYLE = "italic bright_black"

# Timing. FRAME_DELAY * rows + TAGLINE_DELAY stays under the 2.5s budget:
# 6 rows * 0.16 + 0.45 + 0.10 settle is about 1.5 seconds.
FRAME_DELAY = 0.16
TAGLINE_DELAY = 0.45


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _interpolate(start: str, end: str, t: float) -> str:
    a, b = _hex_to_rgb(start), _hex_to_rgb(end)
    mix = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#{:02x}{:02x}{:02x}".format(*mix)


def logo_for(unicode: bool) -> tuple[str, ...]:
    return UNICODE_LOGO if unicode else ASCII_LOGO


def gradient_logo(logo: tuple[str, ...], reveal: int | None = None) -> Text:
    """Paint the logo with a vertical gradient, revealing the first rows.

    ``reveal`` limits how many rows are shown (for the row-by-row fade). All
    rows are shown when it is ``None``.
    """
    lines = logo if reveal is None else logo[:reveal]
    total = max(len(logo) - 1, 1)
    text = Text()
    for index, line in enumerate(lines):
        style = _interpolate(GRADIENT_TOP, GRADIENT_BOTTOM, index / total)
        text.append(f"{line}\n", style=style)
    return text


def tagline_text() -> Text:
    return Text(TAGLINE, style=TAGLINE_STYLE)


def intro_frames(logo: tuple[str, ...]) -> Iterator[RenderableType]:
    """The animated intro frames: the wordmark rows fade in, then the tagline."""
    for row in range(1, len(logo) + 1):
        yield Align.center(gradient_logo(logo, reveal=row))
    final = Group(Align.center(gradient_logo(logo)), Align.center(tagline_text()))
    yield final
    yield final


def render_intro(
    console: Console, *, animate: bool, unicode: bool, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Play the brand intro, or print it statically when animation is off.

    ``animate=False`` is the path for a non-TTY, ``NO_COLOR`` and
    ``--no-animation``: the wordmark and tagline still appear, with no delay.
    """
    logo = logo_for(unicode)
    if not animate:
        console.print(gradient_logo(logo))
        console.print(tagline_text())
        console.print()
        return

    from rich.live import Live

    frames = list(intro_frames(logo))
    with Live(console=console, refresh_per_second=30, transient=False) as live:
        for index, frame in enumerate(frames):
            live.update(frame)
            # the tagline frame gets a beat longer so it reads
            sleep(TAGLINE_DELAY if index >= len(logo) else FRAME_DELAY)
    console.print()
