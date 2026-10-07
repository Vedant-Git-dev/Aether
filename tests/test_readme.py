"""Guards against the README's top heading getting corrupted again — a past
merge once left stray text ('  /us# Aether') in front of it."""

from __future__ import annotations

from pathlib import Path

_README = Path(__file__).resolve().parent.parent / "README.md"


def test_readme_starts_with_the_project_heading() -> None:
    first_line = _README.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "# Aether"
