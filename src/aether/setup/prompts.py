"""The keyboard-driven prompts.

Built directly on prompt_toolkit so arrow keys, Space and Enter behave the same
on Linux and Windows and so the circle indicators are under our control:

- single choice: ``○`` on every option, ``●`` (green) on the option under the
  cursor; Enter confirms and advances
- multi-select: ``○`` unselected, ``●`` (green) for each toggled option, so
  several can be green at once; Space toggles and Enter confirms
- the row under the cursor is highlighted independently of the selected state,
  so "where I am" is never confused with "what I picked"

When the font cannot render the circles the glyphs fall back to ``(*)`` /
``( )``; the caller prints the one-line notice. When color is off the glyphs
stay and only the green is dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style
from rich.console import Console

from .capabilities import Glyphs
from .theme import prompt_style


class GoBack(Exception):
    """Esc or Backspace at a question: go to the previous step."""


class Cancelled(Exception):
    """Ctrl+C at any step: stop cleanly."""


@dataclass
class UI:
    """Everything a prompt needs to draw itself."""

    console: Console
    glyphs: Glyphs
    color: bool
    style: Style | None = None
    input: Input | None = None
    output: Output | None = None

    def __post_init__(self) -> None:
        if self.style is None:
            self.style = prompt_style(self.color)


def hint_line(multi: bool, unicode: bool) -> str:
    arrows = "↑↓" if unicode else "up/down"
    dot = " · " if unicode else " | "
    if multi:
        return f"{arrows} move{dot}space toggle{dot}enter continue{dot}esc back"
    return f"{arrows} move{dot}enter continue{dot}esc back"


def _classes(*names: str) -> str:
    return " ".join(name for name in names if name)


class ChoiceList:
    """The shared single/multi choice application."""

    def __init__(
        self,
        ui: UI,
        question: str,
        options: Sequence[str],
        *,
        multi: bool,
        default_index: int = 0,
        default_selected: set[int] | None = None,
    ) -> None:
        if not options:
            raise ValueError("a choice prompt needs at least one option")
        self.ui = ui
        self.question = question
        self.options = list(options)
        self.multi = multi
        self.cursor = max(0, min(default_index, len(self.options) - 1))
        self.selected: set[int] = set(default_selected or ())
        if not multi:
            self.selected = {self.cursor}

    def _fragments(self) -> FormattedText:
        glyphs = self.ui.glyphs
        rows: list[tuple[str, str]] = []
        # A blank line before the question, and another after it, so the
        # question never crowds the options or the previous output.
        rows.append(("", "\n"))
        rows.append(("class:question", self.question))
        rows.append(("", "\n\n"))
        for index, label in enumerate(self.options):
            on_cursor = index == self.cursor
            chosen = index in self.selected
            pointer = f"{glyphs.pointer} " if on_cursor else "  "
            circle = glyphs.selected if chosen else glyphs.unselected
            on_cls = "class:circle-on" if chosen else ""
            label_cls = "class:choice-on" if chosen else ""
            cursor_cls = "class:cursor" if on_cursor else ""
            rows.append(("class:pointer" if on_cursor else "", pointer))
            rows.append((_classes(cursor_cls, on_cls), circle))
            rows.append((cursor_cls, " "))
            rows.append((_classes(cursor_cls, label_cls), label))
            rows.append(("", "\n"))
        rows.append(("", "\n"))
        if self.multi:
            rows.append(("class:count", f"{len(self.selected)} selected"))
            rows.append(("", "\n\n"))
        rows.append(("class:hint", hint_line(self.multi, glyphs.unicode)))
        rows.append(("", "\n\n"))
        return FormattedText(rows)

    def _build(self) -> Application:
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _up(event: Any) -> None:
            self.cursor = (self.cursor - 1) % len(self.options)
            if not self.multi:
                self.selected = {self.cursor}

        @kb.add("down")
        @kb.add("c-n")
        def _down(event: Any) -> None:
            self.cursor = (self.cursor + 1) % len(self.options)
            if not self.multi:
                self.selected = {self.cursor}

        @kb.add("space")
        def _space(event: Any) -> None:
            if self.multi:
                self.selected.symmetric_difference_update({self.cursor})
            else:
                event.app.exit(result=self.cursor)

        @kb.add("enter")
        def _enter(event: Any) -> None:
            event.app.exit(result=self.cursor)

        @kb.add("escape")
        @kb.add("c-h")  # Backspace
        def _back(event: Any) -> None:
            event.app.exit(exception=GoBack())

        @kb.add("c-c", eager=True)
        def _cancel(event: Any) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        control = FormattedTextControl(self._fragments)
        return Application(
            layout=Layout(Window(control, dont_extend_height=True)),
            key_bindings=kb,
            style=self.ui.style,
            full_screen=False,
            erase_when_done=False,
            input=self.ui.input,
            output=self.ui.output,
        )

    def run(self) -> int | list[int]:
        result = self._build().run()
        if self.multi:
            return sorted(self.selected)
        assert isinstance(result, int)
        return result


def select_one(
    ui: UI,
    question: str,
    options: Sequence[str],
    *,
    default_index: int = 0,
) -> int:
    """A single-choice prompt. Returns the chosen index."""
    return ChoiceList(ui, question, options, multi=False, default_index=default_index).run()  # type: ignore[return-value]


def select_many(
    ui: UI,
    question: str,
    options: Sequence[str],
    *,
    default_selected: set[int] | None = None,
) -> list[int]:
    """A multi-select prompt. Returns the sorted chosen indices."""
    return ChoiceList(ui, question, options, multi=True, default_selected=default_selected).run()  # type: ignore[return-value]


def ask_text(ui: UI, question: str, *, default: str = "") -> str:
    """A plain text input, only for answers no fixed choice can cover."""
    suffix = f" [{default}]" if default else ""
    session: PromptSession = PromptSession(style=ui.style, input=ui.input, output=ui.output)
    answer = session.prompt(f"\n{question}{suffix}: ")
    return answer.strip() or default


def ask_secret(ui: UI, question: str) -> str:
    """A masked input. Empty input returns "" (the caller may treat it as skip)."""
    session: PromptSession = PromptSession(
        style=ui.style, input=ui.input, output=ui.output, is_password=True
    )
    return session.prompt(f"\n{question}: ").strip()
