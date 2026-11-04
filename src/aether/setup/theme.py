"""Shared styling for the wizard.

One accent, one theme, one console factory. The circle green is rich's plain
``green`` (ANSI 32) which stays readable on dark and light terminal themes;
``bright`` variants wash out on light backgrounds. The cursor row is marked
with bold + underline and a cyan pointer rather than reverse video, so "where I
am" never turns the green foreground into a green background bar.
"""

from __future__ import annotations

from typing import TextIO

from prompt_toolkit.styles import Style
from rich.console import Console
from rich.theme import Theme

from .capabilities import color_enabled

# The selection green. Readable on dark and light backgrounds.
GREEN = "green"
GREEN_PT = "ansigreen"
ACCENT_PT = "ansicyan"

AETHER_THEME = Theme(
    {
        "aether.brand": GREEN,
        "aether.muted": "bright_black",
        "aether.heading": "bold",
        "aether.hint": "bright_black",
        "aether.ok": GREEN,
        "aether.warn": "yellow",
        "aether.stop": "red",
    }
)


def build_console(stream: TextIO | None = None, *, color: bool | None = None) -> Console:
    """A rich Console that never emits color unless the terminal supports it."""
    if color is None:
        color = color_enabled(stream)
    return Console(
        file=stream,
        theme=AETHER_THEME,
        no_color=not color,
        force_terminal=color or None,
        highlight=False,
        soft_wrap=True,
    )


def prompt_style(color: bool) -> Style:
    """The prompt_toolkit style for the circle prompts.

    The cursor row gets bold + underline and a cyan pointer; selected rows get
    a green circle and green label. No reverse video: it inverted the green and
    produced an unreadable bar.
    """
    if color:
        return Style.from_dict(
            {
                "question": "bold ansicyan",
                "pointer": "bold ansicyan",
                "circle-on": GREEN_PT,
                "choice-on": GREEN_PT,
                "cursor": "bold underline",
                "hint": "ansibrightblack",
                "count": GREEN_PT,
            }
        )
    return Style.from_dict(
        {
            "question": "bold",
            "pointer": "bold",
            "circle-on": "",
            "choice-on": "",
            "cursor": "bold underline",
            "hint": "",
            "count": "",
        }
    )
