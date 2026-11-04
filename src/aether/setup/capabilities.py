"""Terminal capability detection and glyph selection.

The circle indicators only read well on a Unicode-capable terminal with a font
that has the glyphs. When the console cannot show them we fall back to
``(*)`` / ``( )`` and say so once. Color is dropped when ``NO_COLOR`` is set or
stdout is not a TTY, but the circle glyphs stay so the selection is still
visible.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class Glyphs:
    """The characters the prompts draw with."""

    selected: str
    unselected: str
    pointer: str
    unicode: bool


# Circular indicators, with an ASCII fallback for fonts that lack them.
_UNICODE = Glyphs(selected="●", unselected="○", pointer="❯", unicode=True)
_ASCII = Glyphs(selected="(*)", unselected="( )", pointer=">", unicode=False)


def stdout_is_tty(stream: TextIO | None = None) -> bool:
    """True when the stream is an interactive terminal."""
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def color_enabled(stream: TextIO | None = None, env: dict[str, str] | None = None) -> bool:
    """True when styled output is safe: a TTY and NO_COLOR is not set."""
    env = env if env is not None else os.environ
    if env.get("NO_COLOR"):
        return False
    return stdout_is_tty(stream)


def _encoding_supports_unicode(stream: TextIO | None = None) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    return "utf" in encoding.lower()


def unicode_supported(stream: TextIO | None = None, env: dict[str, str] | None = None) -> bool:
    """True when the terminal is likely to render ● and ○.

    A TTY whose encoding declares UTF is the best signal we have and matches
    the terminals the wizard targets on Linux and modern Windows Terminal.
    Anything else falls back to ASCII.
    """
    env = env if env is not None else os.environ
    if not stdout_is_tty(stream):
        return False
    if env.get("AETHER_ASCII"):
        return False
    return _encoding_supports_unicode(stream)


def glyphs(stream: TextIO | None = None, env: dict[str, str] | None = None) -> Glyphs:
    return _UNICODE if unicode_supported(stream, env) else _ASCII
