"""The setup wizard.

A keyboard-driven terminal UI that replaces the old typed prompts in
``scripts/setup.sh`` and ``scripts/setup.bat``. Every choice is drawn with a
circle indicator, arrow keys move the highlight, Space toggles multi-select
rows, and Enter confirms. Secrets are masked. The wizard is non-interactive
when ``--yes`` or ``--answers FILE`` is passed, so scripted installs work
without a TTY.

The modules, top to bottom:

- ``brand``      the wordmark, tagline and colors (change them in one place)
- ``capabilities`` TTY / color / glyph detection
- ``theme``      the rich console and style
- ``prompts``    the prompt_toolkit circle prompts
- ``catalogs``   the choices read from the repo
- ``plan``       the answers dataclass, TOML and env-file I/O
- ``stages``     the ordered question flow
- ``wizard``     the orchestrator (back-navigation, non-interactive mode)
- ``requirements`` the dependency preflight the launchers run
- ``cli``        argument parsing and the entry point

``__init__`` imports nothing heavy so ``python -m aether.setup.requirements``
works before ``rich`` and ``prompt_toolkit`` are installed.
"""

from __future__ import annotations


def main(*args, **kwargs):
    """Run the wizard (imported lazily so the preflight works without rich)."""
    from .cli import main as _main

    return _main(*args, **kwargs)


__all__ = ["main"]
